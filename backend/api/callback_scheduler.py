"""
Scheduler de Callbacks — verifica a cada minuto se há callbacks pendentes
e dispara ligações automaticamente via ElevenLabs.
"""

import os
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from db.database import SessionLocal, Callback, Lead
from voice.dialer import fazer_ligacao

BRT = timezone(timedelta(hours=-3))

# Variáveis de configuração da IA
IA_NAME = os.getenv("IA_NAME", "Julia")
EMPRESA_NOME = os.getenv("EMPRESA_NOME", "FLC Bank")


def _agora_brt() -> datetime:
    return datetime.now(BRT).replace(tzinfo=None)


def _extrair_callback_da_transcricao(transcricao: str, lead_name: str = "") -> dict | None:
    """
    Usa GPT pra detectar se o cliente pediu pra ligar depois.
    Retorna {"horario": "15:00", "motivo": "..."} ou None.
    """
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    agora = _agora_brt()
    hora_atual = agora.strftime("%H:%M")

    prompt = f"""Analise esta transcrição de ligação comercial.
O cliente OU ALGUÉM QUE ATENDEU (secretária, assistente) pediu pra ligar de volta em algum horário específico?

HORÁRIO ATUAL: {hora_atual}

Exemplos de frases que indicam callback COM horário:
- "me liga às 15h" → horario: "15:00"
- "pode ligar depois das 14?" → horario: "14:00"
- "me liga amanhã de manhã" → horario: "09:00", periodo: "amanha"
- "estou em reunião, liga às 16h" → horario: "16:00"
- Secretária: "tenta às 14h que ele tá livre" → horario: "14:00"
- Julia confirma: "vou ligar às 15h" → horario: "15:00"
- Julia confirma: "combinado, te ligo às 17h" → horario: "17:00"

TEMPOS RELATIVOS (CALCULE com base no horário atual {hora_atual}):
- "daqui a 20 minutos" → some 20 minutos ao horário atual {hora_atual}
- "daqui a 1 hora" → some 1 hora ao horário atual {hora_atual}
- "daqui a 2 horas" → some 2 horas ao horário atual {hora_atual}
- "daqui a meia hora" → some 30 minutos ao horário atual {hora_atual}
Exemplo: se são {hora_atual} e o cliente diz "daqui a 20 min", o horário é {(agora + timedelta(minutes=20)).strftime("%H:%M")}

Exemplos de frases VAGAS (NÃO é callback):
- "me liga daqui a pouco" sem horário confirmado depois → NÃO é callback
- "liga depois" sem horário confirmado depois → NÃO é callback
Só conte como callback se houver um HORÁRIO ESPECÍFICO confirmado na conversa.

Se alguém indicou horário ESPECÍFICO confirmado, responda APENAS em JSON:
{{"callback": true, "horario": "HH:MM", "periodo": "hoje|amanha", "motivo": "frase que indicou o horário"}}

Se NÃO houve horário específico confirmado, responda:
{{"callback": false}}

Transcrição:
{transcricao}

Responda APENAS o JSON, sem texto antes ou depois."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0
        )
        import json
        texto = resp.choices[0].message.content.strip()
        texto = texto.replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto)

        if resultado.get("callback"):
            return resultado
    except Exception as e:
        print(f"⚠️ Erro ao extrair callback: {e}")

    return None


def agendar_callback(lead_id: str, horario_str: str, periodo: str, motivo: str, db: Session) -> Callback | None:
    """
    Cria um callback agendado no banco.
    horario_str: "15:00" ou "14:30"
    periodo: "hoje" ou "amanha"
    """
    try:
        agora = _agora_brt()
        hora, minuto = map(int, horario_str.split(":"))

        if periodo == "amanha":
            data_callback = (agora + timedelta(days=1)).replace(hour=hora, minute=minuto, second=0)
        else:
            data_callback = agora.replace(hour=hora, minute=minuto, second=0)
            # Se o horário já passou hoje, agenda pra amanhã
            if data_callback <= agora:
                data_callback += timedelta(days=1)

        # Verifica se não é fim de semana
        while data_callback.weekday() >= 5:
            data_callback += timedelta(days=1)

        callback = Callback(
            lead_id=lead_id,
            scheduled_at=data_callback,
            status="pendente",
            motivo=motivo,
        )
        db.add(callback)
        db.commit()

        print(f"📅 Callback agendado: {lead_id} → {data_callback.strftime('%d/%m %H:%M')} — {motivo}")
        return callback

    except Exception as e:
        print(f"❌ Erro ao agendar callback: {e}")
        return None


async def executar_callbacks():
    """
    Loop infinito que verifica a cada 60s se há callbacks pendentes.
    Se houver, dispara a ligação via ElevenLabs.
    """
    print("🔄 Scheduler de callbacks iniciado")

    while True:
        try:
            db = SessionLocal()
            agora = _agora_brt()

            # Busca callbacks pendentes cujo horário já chegou
            pendentes = (
                db.query(Callback)
                .filter(
                    Callback.status == "pendente",
                    Callback.scheduled_at <= agora,
                    Callback.tentativas < 3,
                )
                .all()
            )

            for cb in pendentes:
                lead = db.get(Lead, cb.lead_id)
                if not lead:
                    cb.status = "cancelado"
                    db.commit()
                    continue

                # Não liga se o lead já está em outro estado avançado
                if lead.stage in ("agendado", "concluido", "sem_interesse"):
                    cb.status = "cancelado"
                    print(f"⏭️ Callback cancelado: {lead.name} já está em stage {lead.stage}")
                    db.commit()
                    continue

                print(f"📞 Executando callback: {lead.name} ({lead.phone}) — agendado pra {cb.scheduled_at.strftime('%H:%M')}")

                try:
                    conversation_id = fazer_ligacao(lead.phone, lead.name or "", lead.company or "", lead.cnpj or "")
                    lead.stage = "ligando"
                    lead.call_attempts += 1
                    lead.last_call_at = datetime.utcnow()
                    lead.call_sid = conversation_id
                    cb.status = "executado"
                    cb.tentativas += 1
                    print(f"✅ Callback executado: {lead.name} — conv: {conversation_id}")
                except Exception as e:
                    cb.tentativas += 1
                    if cb.tentativas >= 3:
                        cb.status = "falhou"
                    print(f"❌ Callback falhou: {lead.name} — {e}")

                db.commit()

            db.close()
        except Exception as e:
            print(f"❌ Erro no scheduler de callbacks: {e}")

        await asyncio.sleep(60)  # Verifica a cada 60 segundos


# ── FOLLOW-UP SEMANAL — leads interessados ────────────────────────────────────

def _gerar_msg_followup(lead_name: str, resumo: str = "", produto: str = "") -> str:
    """Usa GPT pra gerar mensagem de follow-up personalizada."""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    contexto = ""
    if resumo:
        contexto += f"Resumo da conversa anterior: {resumo}\n"
    if produto:
        contexto += f"Produto de interesse: {produto}\n"

    prompt = f"""Você é {IA_NAME}, consultora da {EMPRESA_NOME}. Gere UMA mensagem curta de follow-up
para enviar pelo WhatsApp a um cliente que demonstrou interesse mas ainda não agendou reunião.

Nome do cliente: {lead_name or 'cliente'}
{contexto}

Regras:
- Máximo 3 frases
- Tom amigável e leve, não pressione
- Pergunte se ainda tem interesse ou se surgiu alguma dúvida
- NÃO repita informações da conversa anterior
- NÃO mencione produtos específicos a menos que ele tenha mencionado
- Use emoji com moderação (máximo 1)
- Termine com uma pergunta aberta

Responda APENAS a mensagem, sem aspas nem explicação."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.8
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️ Erro ao gerar follow-up: {e}")
        nome = lead_name or ""
        return (
            f"Oi{' ' + nome if nome else ''}! Aqui é a {IA_NAME} da {EMPRESA_NOME}. 😊\n\n"
            f"Estou passando pra saber se você ainda tem interesse em conversar "
            f"sobre as soluções de crédito que comentamos. Posso te ajudar em algo?"
        )


async def executar_followups():
    """
    Loop que roda 1x por dia (a cada 6h verifica).
    Envia 1 mensagem por semana pra leads com stage 'interessado'
    que não receberam follow-up nos últimos 7 dias.
    """
    from db.database import WppMensagem
    from integrations.whatsapp import _enviar, _formatar_numero

    print("🔄 Scheduler de follow-ups iniciado")

    while True:
        try:
            db = SessionLocal()
            agora = _agora_brt()

            # Só roda entre 9h e 18h (horário comercial)
            if agora.hour < 9 or agora.hour >= 18:
                db.close()
                await asyncio.sleep(3600)
                continue

            # Só roda em dias úteis
            if agora.weekday() >= 5:
                db.close()
                await asyncio.sleep(3600)
                continue

            # Busca leads interessados
            interessados = (
                db.query(Lead)
                .filter(Lead.stage == "interessado")
                .all()
            )

            enviados = 0
            for lead in interessados:
                # Verifica última mensagem enviada (assistant) nos últimos 7 dias
                sete_dias = agora - timedelta(days=7)
                ultima_msg = (
                    db.query(WppMensagem)
                    .filter(
                        WppMensagem.lead_id == lead.id,
                        WppMensagem.role == "assistant",
                        WppMensagem.created_at >= sete_dias,
                    )
                    .first()
                )

                if ultima_msg:
                    continue  # Já mandou msg nos últimos 7 dias, pula

                # Envia via template (fora da janela de 24h)
                numero = lead.wpp_phone or lead.phone or ""
                if not numero:
                    continue

                numero_fmt = _formatar_numero(numero)
                from integrations.whatsapp import enviar_followup_semanal, IA_NAME, EMPRESA_NOME
                wamid = enviar_followup_semanal(numero_fmt, lead.name or "")

                # Salva no inbox (texto legível)
                nome = lead.name or ""
                msg = (
                    f"Oi{' ' + nome if nome else ''}! Aqui é a {IA_NAME} da {EMPRESA_NOME}.\n\n"
                    f"Estou passando pra saber se você ainda tem interesse em conversar "
                    f"sobre as soluções de crédito que comentamos. Posso te ajudar em algo?"
                )
                wpp_msg = WppMensagem(
                    lead_id=lead.id,
                    role="assistant",
                    content=msg,
                    wamid=wamid,
                    status="sent",
                )
                db.add(wpp_msg)
                db.commit()

                enviados += 1
                print(f"📨 Follow-up enviado: {lead.name} ({numero})")

                # Pausa entre envios pra não sobrecarregar
                await asyncio.sleep(10)

            if enviados > 0:
                print(f"📨 Follow-ups do dia: {enviados} enviados")

            db.close()
        except Exception as e:
            print(f"❌ Erro no scheduler de follow-ups: {e}")

        # Verifica a cada 6 horas
        await asyncio.sleep(6 * 3600)