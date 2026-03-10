from fastapi import APIRouter, Depends, Request, BackgroundTasks, Form
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db.database import get_db, Lead, CallSession, Meeting, WppMensagem
from voice.dialer import fazer_ligacao
from voice.brain import classificar_lead
from integrations.whatsapp import enviar_whatsapp, enviar_confirmacao_agendamento, enviar_agendamento_whatsapp
from db.database import normalizar_telefone
from datetime import datetime
import os, json, asyncio, uuid, re

router = APIRouter()

ELEVENLABS_WEBHOOK_SECRET = os.getenv("ELEVENLABS_WEBHOOK_SECRET", "")


# ─── FUNÇÕES AUXILIARES ──────────────────────────────────────────────────────

def _get_wpp_phone(lead) -> str:
    """Retorna o melhor número pra mandar WhatsApp: wpp_phone se existir, senão phone."""
    return lead.wpp_phone if lead.wpp_phone and lead.wpp_phone.strip() else lead.phone


def _extrair_whatsapp_da_transcricao(transcricao: str, phone_original: str = "") -> str | None:
    """
    Analisa a transcrição da ligação pra ver se o cliente passou um
    número de WhatsApp diferente do que foi ligado.
    
    Se o número extraído não tem DDD, usa o DDD do telefone original.
    """
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    "Analise essa transcrição de ligação. A Sofia perguntou se o número "
                    "da ligação tem WhatsApp.\n\n"
                    "REGRAS:\n"
                    "- Se o cliente disse que o MESMO número tem WhatsApp → responda: MESMO\n"
                    "- Se o cliente deu um número DIFERENTE → extraia TODOS os dígitos (com DDD)\n"
                    "  Ex: 'cinquenta e um, nove nove sete quatro seis...' → 51997464857\n"
                    "  Ex: 'nove nove sete quatro seis quatro oito cinco sete' → 997464857\n"
                    "  Ex: 'meu WhatsApp é 51 99746-4857' → 51997464857\n"
                    "- Se o cliente NÃO quis passar WhatsApp → responda: NENHUM\n"
                    "- Se não ficou claro ou não perguntaram → responda: MESMO\n\n"
                    "Responda APENAS com o número (dígitos) ou MESMO ou NENHUM. Nada mais.\n\n"
                    f"TRANSCRIÇÃO:\n{transcricao[-2000:]}"
                )
            }],
            max_tokens=30,
            temperature=0
        )
        
        resultado = resp.choices[0].message.content.strip()
        print(f"📱 Extração WhatsApp: '{resultado}'")
        
        if resultado in ("MESMO", "NENHUM"):
            return None
        
        # Limpa o número extraído
        digits = "".join(c for c in resultado if c.isdigit())
        
        if len(digits) < 8:
            print(f"⚠️ Número extraído muito curto: '{digits}' — ignorando")
            return None
        
        # Se não tem DDD (8-9 dígitos), pega o DDD do telefone original
        if len(digits) <= 9 and phone_original:
            ddd_original = "".join(c for c in phone_original if c.isdigit())
            if ddd_original.startswith("55") and len(ddd_original) >= 4:
                ddd = ddd_original[2:4]  # pega o DDD (posição 2-3)
                digits = ddd + digits
                print(f"📱 DDD não informado, usando DDD do telefone original: {ddd}")
        
        return normalizar_telefone(digits)
        
    except Exception as e:
        print(f"⚠️ Erro ao extrair WhatsApp da transcrição: {e}")
        return None

def _salvar_msg_wpp(lead_id: str, role: str, content: str, db: Session):
    """
    Salva a mensagem na tabela wpp_mensagens.
    Não faz commit — quem chama decide quando commitar.
    """
    msg = WppMensagem(
        id=str(uuid.uuid4()),
        lead_id=lead_id,
        role=role,
        content=content,
    )
    db.add(msg)


def _montar_msg_nao_atendeu(nome: str) -> str:
    """Monta a mensagem exata que será enviada E salva no banco."""
    saudacao = f"Olá {nome.strip()}! 👋 " if nome and nome.strip() else "Olá! 👋 "
    return (
        saudacao +
        "Aqui é a Sofia da FLC Bank. "
        "Tentei te ligar agora mas não consegui falar com você. "
        "Liguei porque a gente é especializada em crédito para negativados e reestruturação financeira de empresas — "
        "trabalhamos com mais de 60 instituições e conseguimos opções que banco tradicional não oferece. "
        "Posso te explicar melhor como funciona?"
    )


def _tratar_nao_atendeu(lead, db: Session):
    """
    Centraliza a lógica de 'não atendeu'.
    
    ✅ CORREÇÃO PRINCIPAL: A MESMA mensagem é salva no banco E enviada no WhatsApp.
    Antes, `enviar_whatsapp()` montava uma mensagem diferente da salva em `conversa_estado`,
    causando desconexão entre o que o cliente recebeu e o que a IA pensava ter dito.
    """
    lead.stage     = "nao_atendeu"
    lead.wpp_etapa = "pos_ligacao"

    msg_inicial = _montar_msg_nao_atendeu(lead.name)

    # Salva na tabela wpp_mensagens (fonte da verdade)
    _salvar_msg_wpp(lead.id, "assistant", msg_inicial, db)

    # Mantém conversa_estado como backup
    lead.conversa_estado = json.dumps(
        [{"role": "assistant", "content": msg_inicial}],
        ensure_ascii=False
    )

    db.commit()

    # ✅ Envia a MESMA mensagem que foi salva no banco
    numero_wpp = _get_wpp_phone(lead)
    enviar_whatsapp(numero_wpp, lead.name, mensagem=msg_inicial)


class LigarPayload(BaseModel):
    lead_id: Optional[str] = None
    phone:   Optional[str] = None
    name:    Optional[str] = ""

class LotePayload(BaseModel):
    intervalo_segundos: int = 30
    limite: Optional[int] = None


@router.post("/ligar")
def ligar(dados: LigarPayload, db: Session = Depends(get_db)):
    if dados.lead_id:
        lead = db.get(Lead, dados.lead_id)
        if not lead:
            return {"erro": "Lead não encontrado"}

        if lead.stage not in ("novo", "nao_atendeu", "ligando"):
            return {"erro": f"Lead já foi contatado — stage atual: {lead.stage}"}

        print(f"📞 Tentando ligar para {lead.name} ({lead.phone}) — stage: {lead.stage}")
        try:
            conversation_id = fazer_ligacao(lead.phone, lead.name or "")
        except Exception as e:
            print(f"❌ Erro ao ligar para {lead.name}: {e}")
            return {"erro": f"Falha na ligação: {str(e)}"}

        lead.stage         = "ligando"
        lead.call_attempts += 1
        lead.last_call_at  = datetime.utcnow()
        lead.call_sid      = conversation_id
        db.commit()
        print(f"✅ Ligação iniciada para {lead.name} — conv: {conversation_id}")
        return {"mensagem": "Ligação iniciada!", "conversation_id": conversation_id, "lead": lead.name}
    else:
        phone_limpo = "".join(c for c in (dados.phone or "") if c.isdigit())
        lead_existente = db.query(Lead).filter(Lead.phone.contains(phone_limpo[-8:])).first()
        if lead_existente and lead_existente.stage not in ("novo", "nao_atendeu"):
            return {"erro": f"Número já existe no CRM — stage: {lead_existente.stage}"}

        conversation_id = fazer_ligacao(dados.phone or "", dados.name or "")
        return {"mensagem": "Ligação iniciada!", "conversation_id": conversation_id}


@router.post("/disparar-lote")
async def disparar_lote(
    dados: LotePayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    query = db.query(Lead).filter(Lead.stage == "novo")
    if dados.limite:
        query = query.limit(dados.limite)
    leads = query.all()

    if not leads:
        return {"mensagem": "Nenhum lead novo encontrado", "total": 0}

    lead_ids = [l.id for l in leads]
    background_tasks.add_task(_executar_lote, lead_ids, dados.intervalo_segundos)

    return {
        "mensagem": "Disparando lote em background!",
        "total_leads": len(lead_ids),
        "intervalo_segundos": dados.intervalo_segundos,
        "tempo_estimado_minutos": round((len(lead_ids) * dados.intervalo_segundos) / 60, 1)
    }


async def _executar_lote(lead_ids: list, intervalo: int):
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        for i, lead_id in enumerate(lead_ids):
            lead = db.get(Lead, lead_id)
            if not lead or lead.stage != "novo":
                continue
            try:
                print(f"📞 Lote [{i+1}/{len(lead_ids)}] — {lead.name} ({lead.phone})")
                conv_id = fazer_ligacao(lead.phone, lead.name or "")
                lead.stage         = "ligando"
                lead.call_attempts += 1
                lead.last_call_at  = datetime.utcnow()
                lead.call_sid      = conv_id
                db.commit()
            except Exception as e:
                print(f"   ❌ Erro: {e}")
            if i < len(lead_ids) - 1:
                await asyncio.sleep(intervalo)
    finally:
        db.close()


@router.post("/pos-chamada")
async def pos_chamada(request: Request, db: Session = Depends(get_db)):
    body_bytes = await request.body()
    payload = json.loads(body_bytes)
    print(f"📥 Webhook pós-chamada recebido")

    data        = payload.get("data", {})
    metadata    = data.get("metadata", {})
    phone_call  = metadata.get("phone_call", {})

    conversation_id = data.get("conversation_id", "")
    twilio_call_sid = phone_call.get("call_sid", "")
    status          = data.get("status", "done")
    duracao         = metadata.get("call_duration_secs", 0)
    transcricao_raw = data.get("transcript", [])

    print(f"   conversation_id: {conversation_id}")
    print(f"   twilio_call_sid: {twilio_call_sid}")
    print(f"   duração: {duracao}s | turnos: {len(transcricao_raw)}")

    lead = (
        db.query(Lead).filter(Lead.call_sid == conversation_id).first() or
        db.query(Lead).filter(Lead.call_sid == twilio_call_sid).first()
    )

    if not lead:
        print(f"⚠️ Lead não encontrado — conv: {conversation_id} | twilio: {twilio_call_sid}")
        return {"ok": True, "aviso": "Lead não encontrado"}

    # ── NÃO ATENDEU ──────────────────────────────────────────────────────
    if duracao < 25 or status in ("no-answer", "busy", "failed"):
        _tratar_nao_atendeu(lead, db)
        print(f"📵 {lead.name} não atendeu ({duracao}s / status: {status})")
        return {"ok": True, "acao": "nao_atendeu"}

    # ── Monta transcrição ────────────────────────────────────────────────
    historico = []
    transcricao_texto = ""
    for turno in transcricao_raw:
        role    = "user" if turno.get("role") == "user" else "assistant"
        content = (turno.get("message") or turno.get("content") or "").strip()
        if not content:
            continue
        historico.append({"role": role, "content": content})
        quem = "Cliente" if role == "user" else "Sofia"
        transcricao_texto += f"{quem}: {content}\n"

    # ── Cliente não falou nada ───────────────────────────────────────────
    user_falas = [h for h in historico if h["role"] == "user"]
    if not historico or not user_falas:
        _tratar_nao_atendeu(lead, db)
        print(f"📵 {lead.name} não falou nada — marcado como nao_atendeu")
        return {"ok": True, "acao": "nao_atendeu"}

    # ── Classificação ────────────────────────────────────────────────────
    print(f"🧠 Classificando {lead.name} ({duracao}s de conversa)...")
    classificacao = classificar_lead(historico)
    print(f"   Resultado: {classificacao}")

    lead.conversa      = transcricao_texto
    lead.resumo        = classificacao.get("resumo", "")
    lead.temperature   = classificacao.get("temperatura", "cold")
    lead.product       = classificacao.get("produto") or ""
    lead.desired_value = classificacao.get("valor_desejado") or ""
    lead.urgency       = classificacao.get("urgencia") or ""
    lead.agendado_hora = None

    # ✅ NOVO: Extrai número de WhatsApp da transcrição (se o cliente passou um diferente)
    wpp_extraido = _extrair_whatsapp_da_transcricao(transcricao_texto, phone_original=lead.phone)
    if wpp_extraido:
        lead.wpp_phone = wpp_extraido
        print(f"📱 WhatsApp diferente detectado: {wpp_extraido} (telefone da ligação: {lead.phone})")

    numero_wpp = _get_wpp_phone(lead)

    temp = classificacao.get("temperatura", "cold")

    if temp in ("hot", "warm") or duracao >= 60:
        lead.stage     = "interessado"
        lead.wpp_etapa = "aguardando_tipo"

        # Salva contexto da ligação como nota interna
        resumo_ctx = classificacao.get("resumo", "")
        if resumo_ctx:
            nota_interna = (
                f"[CONTEXTO DA LIGAÇÃO] Duração: {duracao}s. "
                f"Resumo: {resumo_ctx}. "
                f"Produto de interesse: {lead.product or 'não definido'}. "
                f"Temperatura: {temp}."
            )
            _salvar_msg_wpp(lead.id, "system", nota_interna, db)

        saudacao = f"Olá {lead.name}! 😊 " if lead.name else "Olá! 😊 "
        msg_agendamento = (
            saudacao +
            "Aqui é a Sofia da FLC Bank. "
            "Foi ótimo conversar com você! "
            "Vamos marcar sua reunião? "
            "Prefere por ligação telefônica (1) ou vídeo chamada (2)?"
        )
        _salvar_msg_wpp(lead.id, "assistant", msg_agendamento, db)
        enviar_agendamento_whatsapp(numero_wpp, lead.name, mensagem=msg_agendamento)

    elif temp == "cold":
        lead.stage = "sem_interesse"
    else:
        lead.stage = "atendeu"

    sessao = CallSession(
        lead_id      = lead.id,
        twilio_sid   = conversation_id,
        status       = status,
        duration_sec = int(duracao),
        transcript   = transcricao_texto,
        resumo       = lead.resumo,
        resultado    = temp
    )
    db.add(sessao)
    lead.updated_at = datetime.utcnow()
    db.commit()

    print(f"✅ Lead {lead.name} → stage: {lead.stage} | temp: {lead.temperature}")
    return {"ok": True, "stage": lead.stage, "temperatura": lead.temperature}


@router.post("/amd-status")
async def amd_status(
    CallSid: Optional[str] = Form(None),
    AnsweredBy: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    print(f"🤖 AMD: {CallSid} — {AnsweredBy}")

    if AnsweredBy in ("machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"):
        sessao = db.query(CallSession).filter(CallSession.twilio_sid == CallSid).first()
        lead = None
        if sessao:
            lead = db.get(Lead, sessao.lead_id)
        else:
            lead = db.query(Lead).filter(Lead.call_sid == CallSid).first()

        if lead:
            _tratar_nao_atendeu(lead, db)
            print(f"📱 Caixa postal — {lead.name} → nao_atendeu")
        else:
            print(f"⚠️ Lead não encontrado para CallSid: {CallSid}")


@router.post("/verificar-agenda")
async def verificar_agenda(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    data_hora = body.get("data_hora", "")

    ocupado = db.query(Meeting).filter(
        Meeting.scheduled_at == data_hora,
        Meeting.status.in_(["agendado", "em_andamento"])
    ).count() > 0

    if not ocupado:
        return {"disponivel": True, "mensagem": f"Horário {data_hora} está disponível!"}

    from datetime import timedelta
    try:
        base = datetime.strptime(data_hora, "%Y-%m-%d %H:%M")
    except:
        return {"disponivel": False, "mensagem": "Horário ocupado.", "proximos_horarios": []}

    livres = []
    tentativa = base + timedelta(hours=1)
    while len(livres) < 3:
        slot = tentativa.strftime("%Y-%m-%d %H:%M")
        count = db.query(Meeting).filter(
            Meeting.scheduled_at == slot,
            Meeting.status.in_(["agendado", "em_andamento"])
        ).count()
        if count == 0:
            livres.append(tentativa.strftime("%d/%m às %Hh"))
        tentativa += timedelta(hours=1)

    return {
        "disponivel": False,
        "mensagem": f"Horário ocupado. Próximos disponíveis: {', '.join(livres)}",
        "proximos_horarios": livres
    }
