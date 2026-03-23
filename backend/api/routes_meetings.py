import os
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from db.database import get_db, Meeting, Lead
from integrations.daily import obter_gravacoes, criar_sala
from datetime import datetime

router = APIRouter()

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


# ─────────────────────────────────────────────
# PÁGINAS HTML
# ─────────────────────────────────────────────

@router.get("/sala/{room_name}", response_class=HTMLResponse)
def sala_video(room_name: str, db: Session = Depends(get_db)):
    """Sala de vídeo — especialista entra aqui."""
    meeting = db.query(Meeting).filter(Meeting.room_name == room_name).first()
    if not meeting:
        return HTMLResponse("<h1>Sala não encontrada</h1>", status_code=404)

    lead = db.get(Lead, meeting.lead_id) if meeting.lead_id else None

    with open(os.path.join(STATIC_DIR, "sala.html"), "r", encoding="utf-8") as f:
        html = f.read()

    html = html.replace("{{ROOM_URL}}", meeting.room_url or "")
    html = html.replace("{{ROOM_NAME}}", room_name)
    html = html.replace("{{LEAD_NAME}}", lead.name if lead else "Cliente")
    html = html.replace("{{SCHEDULED_AT}}", meeting.scheduled_at or "")
    html = html.replace("{{TOKEN}}", meeting.token_host or "")
    return HTMLResponse(html)


@router.get("/espera/{room_name}", response_class=HTMLResponse)
def sala_espera(room_name: str, db: Session = Depends(get_db)):
    """Waiting room — cliente entra aqui antes da reunião."""
    meeting = db.query(Meeting).filter(Meeting.room_name == room_name).first()
    if not meeting:
        return HTMLResponse("<h1>Reunião não encontrada</h1>", status_code=404)

    lead = db.get(Lead, meeting.lead_id) if meeting.lead_id else None

    with open(os.path.join(STATIC_DIR, "espera.html"), "r", encoding="utf-8") as f:
        html = f.read()

    html = html.replace("{{ROOM_URL}}", meeting.room_url or "")
    html = html.replace("{{ROOM_NAME}}", room_name)
    html = html.replace("{{LEAD_NAME}}", lead.name if lead else "")
    html = html.replace("{{SCHEDULED_AT}}", meeting.scheduled_at or "")
    html = html.replace("{{TOKEN}}", meeting.token_guest or "")
    return HTMLResponse(html)


@router.get("/agenda")
def agenda_especialista():
    """Redireciona para o painel unificado."""
    return RedirectResponse("/dashboard", status_code=302)


# ─────────────────────────────────────────────
# API JSON
# ─────────────────────────────────────────────

@router.get("/api/meetings")
def listar_meetings(db: Session = Depends(get_db)):
    """Lista todos os agendamentos para o painel do especialista."""
    meetings = db.query(Meeting).order_by(Meeting.scheduled_at.asc()).all()
    resultado = []
    for m in meetings:
        lead = db.get(Lead, m.lead_id) if m.lead_id else None
        resultado.append({
            "id": m.id,
            "lead_name": lead.name if lead else "—",
            "lead_phone": lead.phone if lead else "—",
            "scheduled_at": m.scheduled_at,
            "status": m.status,
            "tipo": m.tipo,
            "room_name": m.room_name,
            "link_especialista": m.link_especialista,
            "link_cliente": m.link_cliente,
            "especialista": m.especialista,
            "recording_url": m.recording_url,
        })
    return resultado


@router.get("/api/meetings/hoje")
def meetings_hoje(db: Session = Depends(get_db)):
    """Retorna apenas meetings de hoje."""
    hoje = datetime.now().strftime("%Y-%m-%d")
    meetings = db.query(Meeting).filter(
        Meeting.scheduled_at.startswith(hoje),
        Meeting.status.in_(["agendado", "em_andamento"])
    ).order_by(Meeting.scheduled_at.asc()).all()

    resultado = []
    for m in meetings:
        lead = db.get(Lead, m.lead_id) if m.lead_id else None
        resultado.append({
            "id": m.id,
            "lead_name": lead.name if lead else "—",
            "lead_phone": lead.phone if lead else "—",
            "scheduled_at": m.scheduled_at,
            "status": m.status,
            "tipo": m.tipo,
            "link_especialista": m.link_especialista,
            "room_name": m.room_name,
        })
    return resultado


@router.post("/api/meetings/{meeting_id}/status")
async def atualizar_status(meeting_id: str, request: Request, db: Session = Depends(get_db)):
    """Atualiza status de uma reunião."""
    body = await request.json()
    status = body.get("status")
    especialista = body.get("especialista")

    meeting = db.get(Meeting, meeting_id)
    if not meeting:
        return {"erro": "Reunião não encontrada"}

    if status:
        meeting.status = status
    if especialista:
        meeting.especialista = especialista
    meeting.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "status": meeting.status}


@router.get("/api/meetings/{meeting_id}/gravacoes")
def gravacoes(meeting_id: str, db: Session = Depends(get_db)):
    """Busca gravações de uma reunião no Daily.co."""
    meeting = db.get(Meeting, meeting_id)
    if not meeting:
        return {"erro": "Reunião não encontrada"}

    gravacoes = obter_gravacoes(meeting.room_name)
    return {"gravacoes": gravacoes}





@router.get("/api/meetings/by-room/{room_name}/presenca")
def presenca_sala(room_name: str):
    """Verifica se tem participantes (host) na sala via Daily.co."""
    import requests, os
    api_key = os.getenv("DAILY_API_KEY", "")
    try:
        res = requests.get(
            f"https://api.daily.co/v1/rooms/{room_name}/presence",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5
        )
        if res.status_code == 200:
            data = res.json()
            total = data.get("total_count", 0)
            return {"host_presente": total > 0, "participantes": total}
    except:
        pass
    return {"host_presente": False, "participantes": 0}

@router.get("/api/meetings/by-room/{room_name}")
def meeting_by_room(room_name: str, db: Session = Depends(get_db)):
    """Retorna dados de uma reunião pelo room_name."""
    meeting = db.query(Meeting).filter(Meeting.room_name == room_name).first()
    if not meeting:
        return {"status": "not_found"}
    return {"id": meeting.id, "status": meeting.status, "room_name": meeting.room_name}


@router.post("/api/meetings/by-room/{room_name}/encerrar")
def encerrar_por_room(room_name: str, db: Session = Depends(get_db)):
    """Host encerrou — marca reunião como concluída e fecha a sala no Daily.co."""
    meeting = db.query(Meeting).filter(Meeting.room_name == room_name).first()
    if meeting:
        meeting.status = "concluido"
        meeting.updated_at = datetime.utcnow()
        db.commit()

    # Fecha a sala no Daily.co para todos os participantes
    try:
        import requests
        api_key = os.getenv("DAILY_API_KEY", "")
        if api_key and room_name:
            # Envia eject para todos os participantes
            requests.post(
                f"https://api.daily.co/v1/rooms/{room_name}/eject",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"everyone": True},
                timeout=5
            )
            print(f"🚪 Sala {room_name} — todos os participantes ejetados")
    except Exception as e:
        print(f"⚠️ Erro ao ejetar participantes: {e}")

    return {"ok": True}

@router.post("/api/meetings")
async def criar_meeting_manual(request: Request, db: Session = Depends(get_db)):
    """Cria uma reunião manualmente pelo painel do especialista."""
    body = await request.json()
    nome       = body.get("nome", "Cliente")
    phone      = body.get("phone", "")
    scheduled  = body.get("scheduled_at", "")
    tipo       = body.get("tipo", "video_chamada")
    especialista = body.get("especialista", "")

    # Busca ou cria lead
    lead = None
    if phone:
        phone_limpo = "".join(c for c in phone if c.isdigit())
        lead = db.query(Lead).filter(Lead.phone.contains(phone_limpo[-8:])).first()
    if not lead:
        lead = Lead(name=nome, phone=phone or "", stage="agendado")
        db.add(lead)
        db.flush()
    else:
        # Lead já existe — atualiza stage pra agendado
        lead.stage = "agendado"
        if nome and not lead.name:
            lead.name = nome

    # Atualiza scheduled_at no lead
    lead.agendado_hora = scheduled

    # Normaliza tipo: "video_chamada" → "meet", "ligacao" → "ligacao"
    tipo_normalizado = "meet" if tipo in ("video_chamada", "meet", "video") else "ligacao"

    meeting = Meeting(
        lead_id     = lead.id,
        scheduled_at = scheduled,
        tipo        = tipo_normalizado,
        status      = "agendado",
        especialista = especialista,
    )
    db.add(meeting)
    db.flush()

    # Cria sala Daily.co
    sala = criar_sala(nome, scheduled)
    if sala.get("sucesso"):
        meeting.room_name         = sala["room_name"]
        meeting.room_url          = sala["room_url"]
        meeting.link_cliente      = sala["link_cliente"]
        meeting.link_especialista = sala["link_especialista"]
        meeting.token_host        = sala.get("token_host", "")
        meeting.token_guest       = sala.get("token_guest", "")

    db.commit()
    return {
        "ok": True,
        "meeting_id": meeting.id,
        "link_cliente": meeting.link_cliente,
        "link_especialista": meeting.link_especialista,
    }

@router.delete("/api/meetings/{meeting_id}")
def cancelar_meeting(meeting_id: str, db: Session = Depends(get_db)):
    """Cancela uma reunião."""
    meeting = db.get(Meeting, meeting_id)
    if not meeting:
        return {"erro": "Reunião não encontrada"}
    meeting.status = "cancelado"
    meeting.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────
# API — Gravação e Transcrição
# ─────────────────────────────────────────────

@router.post("/api/meetings/{meeting_id}/iniciar-gravacao")
def api_iniciar_gravacao(meeting_id: str, db: Session = Depends(get_db)):
    """Inicia gravação manualmente."""
    meeting = db.get(Meeting, meeting_id)
    if not meeting or not meeting.room_name:
        return {"erro": "Reunião não encontrada"}

    from integrations.daily import iniciar_gravacao
    resultado = iniciar_gravacao(meeting.room_name)
    return resultado


@router.post("/api/meetings/{meeting_id}/parar-gravacao")
def api_parar_gravacao(meeting_id: str, db: Session = Depends(get_db)):
    """Para gravação manualmente."""
    meeting = db.get(Meeting, meeting_id)
    if not meeting or not meeting.room_name:
        return {"erro": "Reunião não encontrada"}

    from integrations.daily import parar_gravacao
    resultado = parar_gravacao(meeting.room_name)
    return resultado


@router.get("/api/meetings/{meeting_id}/transcricao")
def api_transcricao(meeting_id: str, db: Session = Depends(get_db)):
    """Retorna transcrição e resumo de uma reunião."""
    meeting = db.get(Meeting, meeting_id)
    if not meeting:
        return {"erro": "Reunião não encontrada"}

    return {
        "transcricao": meeting.transcricao_reuniao or "",
        "resumo": meeting.resumo_reuniao or "",
        "recording_url": meeting.recording_url or "",
    }