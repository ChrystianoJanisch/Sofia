"""
Scheduler de lembretes automáticos.
Roda como background task no startup do FastAPI.
Verifica a cada 5 minutos se há reuniões na próxima hora
e envia lembrete no WhatsApp.
"""
import asyncio, os
from datetime import datetime, timedelta, timezone
from db.database import SessionLocal, Meeting, Lead

BR_TZ = timezone(timedelta(hours=-3))

# Variáveis de configuração da IA
IA_NAME = os.getenv("IA_NAME", "Julia")
EMPRESA_NOME = os.getenv("EMPRESA_NOME", "FLC Bank")


def _get_wpp_phone(lead) -> str:
    return lead.wpp_phone if lead.wpp_phone and lead.wpp_phone.strip() else lead.phone


def _enviar_lembrete(lead, meeting, db):
    """Envia lembrete de reunião via WhatsApp e salva no inbox."""
    from integrations.whatsapp import enviar_whatsapp
    from db.database import WppMensagem

    nome = lead.name or "Cliente"
    hora = ""
    try:
        if " " in meeting.scheduled_at:
            hora = meeting.scheduled_at.split(" ")[1][:5]
        else:
            hora = meeting.scheduled_at[11:16]
    except:
        hora = meeting.scheduled_at

    tipo_texto = "vídeo chamada 🎥" if meeting.tipo in ("meet", "video_chamada", "video") else "ligação telefônica 📞"

    msg = (
        f"Olá {nome}! 😊 Aqui é a {IA_NAME} da {EMPRESA_NOME}.\n\n"
        f"Só passando pra lembrar que sua reunião com nosso especialista "
        f"está marcada pra hoje às {hora}.\n\n"
        f"Tipo: {tipo_texto}\n"
    )

    if meeting.link_cliente and meeting.tipo in ("meet", "video_chamada", "video"):
        msg += f"\nAcesse por aqui: {meeting.link_cliente}\n"

    msg += "\nTe esperamos! Qualquer dúvida, é só me chamar aqui. 😊"

    numero = _get_wpp_phone(lead)
    try:
        enviar_whatsapp(numero, nome, mensagem=msg)
        # Salva no inbox
        wpp_msg = WppMensagem(lead_id=lead.id, role="assistant", content=msg)
        db.add(wpp_msg)
        db.commit()
        print(f"🔔 Lembrete enviado para {nome} ({numero}) — reunião às {hora}")
        return True
    except Exception as e:
        print(f"⚠️ Erro ao enviar lembrete para {nome}: {e}")
        return False


async def verificar_lembretes():
    """Verifica se há reuniões na próxima hora que precisam de lembrete."""
    db = SessionLocal()
    try:
        agora_br = datetime.now(BR_TZ).replace(tzinfo=None)
        daqui_1h = agora_br + timedelta(hours=1)
        daqui_10min = agora_br + timedelta(minutes=10)

        # Formato esperado: "2026-03-09 14:00"
        agora_str = agora_br.strftime("%Y-%m-%d %H:%M")
        daqui_1h_str = daqui_1h.strftime("%Y-%m-%d %H:%M")

        meetings = db.query(Meeting).filter(
            Meeting.status == "agendado",
            Meeting.lembrete_enviado == False,
            Meeting.scheduled_at >= agora_str,
            Meeting.scheduled_at <= daqui_1h_str,
        ).all()

        for meeting in meetings:
            lead = db.query(Lead).filter(Lead.id == meeting.lead_id).first()
            if not lead:
                continue

            sucesso = _enviar_lembrete(lead, meeting, db)
            if sucesso:
                meeting.lembrete_enviado = True
                db.commit()

        if meetings:
            print(f"🔔 Verificação de lembretes: {len(meetings)} enviados")

    except Exception as e:
        print(f"⚠️ Erro no scheduler de lembretes: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


async def scheduler_loop():
    """Loop infinito que roda a cada 5 minutos."""
    print("🔔 Scheduler de lembretes iniciado")
    while True:
        try:
            await verificar_lembretes()
        except Exception as e:
            print(f"⚠️ Erro no loop do scheduler: {e}")
        await asyncio.sleep(300)  # 5 minutos


def iniciar_scheduler():
    """Chamado no startup do FastAPI."""
    asyncio.create_task(scheduler_loop())
