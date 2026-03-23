"""
Webhook handler para eventos do Daily.co.
Recebe notificação quando gravação está pronta, baixa o áudio,
transcreve com Whisper e salva no CRM.
"""

import os, json, tempfile, requests
from fastapi import APIRouter, Request
from sqlalchemy.orm import Session
from db.database import get_db, Meeting, Lead, SessionLocal
from datetime import datetime

router = APIRouter()


@router.post("/webhook")
async def daily_webhook(request: Request):
    """Recebe eventos do Daily.co (gravação pronta, reunião encerrada)."""
    try:
        body = await request.json()
    except:
        return {"ok": True}

    event_type = body.get("type", "")
    payload = body.get("payload", {}) or body.get("data", {})

    print(f"📥 Daily.co webhook: {event_type}")

    if event_type == "recording.ready-to-download":
        await _processar_gravacao(payload)

    elif event_type == "meeting.ended":
        room_name = payload.get("room", "") or payload.get("room_name", "")
        if room_name:
            print(f"📋 Reunião encerrada: {room_name}")

    return {"ok": True}


async def _processar_gravacao(payload: dict):
    """
    Quando a gravação está pronta:
    1. Obtém o link de download
    2. Baixa o áudio
    3. Transcreve com Whisper
    4. Salva transcrição e link no Meeting
    """
    recording_id = payload.get("recording_id", "")
    room_name = payload.get("room_name", "")

    if not recording_id or not room_name:
        # Tenta formatos alternativos do payload
        recording_id = payload.get("id", recording_id)
        room_name = payload.get("room", room_name)

    if not recording_id:
        print("⚠️ Gravação sem recording_id — ignorando")
        return

    print(f"🎬 Processando gravação: {recording_id} (sala: {room_name})")

    db = SessionLocal()
    try:
        # Busca meeting pelo room_name
        meeting = None
        if room_name:
            meeting = db.query(Meeting).filter(Meeting.room_name == room_name).first()

        if not meeting:
            print(f"⚠️ Meeting não encontrado para sala {room_name}")
            db.close()
            return

        # 1. Obtém link de download
        from integrations.daily import obter_link_gravacao
        download_url = obter_link_gravacao(recording_id)

        if not download_url:
            print(f"⚠️ Não conseguiu obter link de download para {recording_id}")
            # Salva pelo menos o recording_id
            meeting.recording_url = f"daily-recording:{recording_id}"
            db.commit()
            db.close()
            return

        # Salva URL da gravação
        meeting.recording_url = download_url
        db.commit()
        print(f"💾 URL da gravação salva: {download_url[:80]}...")

        # 2. Baixa o áudio e transcreve
        transcricao = await _transcrever_gravacao(download_url)

        if transcricao:
            # 3. Salva transcrição no meeting
            meeting.transcricao_reuniao = transcricao
            meeting.updated_at = datetime.utcnow()
            db.commit()

            # 4. Gera resumo e salva no lead
            lead = db.get(Lead, meeting.lead_id) if meeting.lead_id else None
            if lead:
                resumo = _gerar_resumo_reuniao(transcricao, lead.name or "")
                if resumo:
                    # Append ao resumo existente
                    resumo_anterior = lead.resumo or ""
                    lead.resumo = (
                        resumo_anterior +
                        f"\n\n[REUNIÃO {datetime.now().strftime('%d/%m %H:%M')}] {resumo}"
                    ).strip()
                    db.commit()
                    print(f"📝 Resumo da reunião salvo para {lead.name}")

            print(f"✅ Transcrição salva: {len(transcricao)} chars")
        else:
            print(f"⚠️ Não conseguiu transcrever gravação {recording_id}")

    except Exception as e:
        print(f"❌ Erro ao processar gravação: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


async def _transcrever_gravacao(download_url: str) -> str:
    """Baixa o áudio da gravação e transcreve com OpenAI Whisper."""
    try:
        # Baixa o arquivo de áudio
        print("⬇️ Baixando gravação...")
        resp = requests.get(download_url, timeout=120, stream=True)
        if resp.status_code != 200:
            print(f"❌ Erro ao baixar gravação: HTTP {resp.status_code}")
            return ""

        # Salva em arquivo temporário
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            for chunk in resp.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name

        file_size = os.path.getsize(tmp_path)
        print(f"📁 Gravação baixada: {file_size / 1024 / 1024:.1f}MB")

        # Limite do Whisper é 25MB
        if file_size > 25 * 1024 * 1024:
            print("⚠️ Arquivo muito grande para Whisper (>25MB) — tentando dividir...")
            transcricao = await _transcrever_arquivo_grande(tmp_path)
            os.unlink(tmp_path)
            return transcricao

        # Transcreve com Whisper
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        print("🎤 Transcrevendo com Whisper...")
        with open(tmp_path, "rb") as audio_file:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="pt",
                response_format="verbose_json",
            )

        os.unlink(tmp_path)

        # Monta transcrição com timestamps
        if hasattr(result, "segments") and result.segments:
            linhas = []
            for seg in result.segments:
                minuto = int(seg.get("start", 0)) // 60
                segundo = int(seg.get("start", 0)) % 60
                texto = seg.get("text", "").strip()
                if texto:
                    linhas.append(f"[{minuto:02d}:{segundo:02d}] {texto}")
            transcricao = "\n".join(linhas)
        else:
            transcricao = result.text if hasattr(result, "text") else str(result)

        print(f"✅ Transcrição concluída: {len(transcricao)} chars")
        return transcricao

    except Exception as e:
        print(f"❌ Erro na transcrição: {e}")
        import traceback
        traceback.print_exc()
        return ""


async def _transcrever_arquivo_grande(file_path: str) -> str:
    """Transcreve arquivos maiores que 25MB usando ffmpeg pra dividir."""
    try:
        import subprocess

        # Divide em chunks de 10 minutos
        chunks_dir = tempfile.mkdtemp()
        subprocess.run([
            "ffmpeg", "-i", file_path,
            "-f", "segment", "-segment_time", "600",
            "-c", "copy",
            f"{chunks_dir}/chunk_%03d.mp4"
        ], capture_output=True, timeout=120)

        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        transcricao_completa = []
        chunk_files = sorted(os.listdir(chunks_dir))

        for i, chunk_file in enumerate(chunk_files):
            chunk_path = os.path.join(chunks_dir, chunk_file)
            print(f"🎤 Transcrevendo chunk {i+1}/{len(chunk_files)}...")

            with open(chunk_path, "rb") as audio:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio,
                    language="pt",
                )
            transcricao_completa.append(result.text)
            os.unlink(chunk_path)

        os.rmdir(chunks_dir)
        return "\n\n".join(transcricao_completa)

    except Exception as e:
        print(f"❌ Erro ao transcrever arquivo grande: {e}")
        return ""


def _gerar_resumo_reuniao(transcricao: str, nome_lead: str = "") -> str:
    """Gera resumo da reunião usando GPT."""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": f"""Resuma esta transcrição de reunião comercial da FLC Bank com o cliente {nome_lead}.

Inclua:
- Principais pontos discutidos
- Produtos/soluções mencionados
- Valores/prazos discutidos (se houver)
- Próximos passos combinados
- Nível de interesse do cliente

Seja objetivo em no máximo 5 linhas.

Transcrição:
{transcricao[:4000]}"""
            }],
            max_tokens=300,
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️ Erro ao gerar resumo: {e}")
        return ""