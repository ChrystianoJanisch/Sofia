from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from db.database import get_db, Lead, CallSession
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta

router = APIRouter()

class AtualizarLeadPayload(BaseModel):
    stage:       Optional[str] = None
    resumo:      Optional[str] = None
    produto:     Optional[str] = None
    temperatura: Optional[str] = None
    agendado_hora: Optional[str] = None
    especialista:  Optional[str] = None
    notes:         Optional[str] = None

@router.get("/pipeline")
def pipeline(db: Session = Depends(get_db)):
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()

    stages = {
        "novo": [],
        "ligando": [],
        "atendeu": [],
        "interessado": [],
        "agendado": [],
        "fechado": [],
        "nao_atendeu": [],
        "sem_interesse": [],
        "arquivado": []
    }

    for l in leads:
        stage = l.stage if l.stage in stages else "novo"
        stages[stage].append({
            "id": l.id,
            "name": l.name,
            "phone": l.phone,
            "stage": l.stage,
            "temperature": l.temperature,
            "product": l.product,
            "resumo": l.resumo,
            "agendado_hora": l.agendado_hora,
            "especialista": l.especialista,
            "call_attempts": l.call_attempts,
            "last_call_at": str(l.last_call_at - timedelta(hours=3)) if l.last_call_at else None,
            "created_at": str(l.created_at)
        })

    totais = {k: len(v) for k, v in stages.items()}
    totais["total"] = len(leads)

    return {"pipeline": stages, "totais": totais}

@router.get("/lead/{lead_id}")
def detalhe_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    sessoes = db.query(CallSession).filter(CallSession.lead_id == lead_id).all()

    return {
        "id": lead.id,
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "stage": lead.stage,
        "temperature": lead.temperature,
        "product": lead.product,
        "resumo": lead.resumo,
        "conversa": lead.conversa,
        "agendado_hora": lead.agendado_hora,
        "especialista": lead.especialista,
        "call_attempts": lead.call_attempts,
        "created_at": str(lead.created_at),
        "sessoes": [
            {
                "id": s.id,
                "status": s.status,
                "duration_sec": s.duration_sec,
                "resumo": s.resumo,
                "resultado": s.resultado,
                "started_at": str(s.started_at)
            }
            for s in sessoes
        ]
    }

@router.put("/lead/{lead_id}")
def atualizar_lead(lead_id: str, dados: AtualizarLeadPayload, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    if dados.stage:        lead.stage = dados.stage
    if dados.resumo:       lead.resumo = dados.resumo
    if dados.produto:      lead.product = dados.produto
    if dados.temperatura:  lead.temperature = dados.temperatura
    if dados.agendado_hora: lead.agendado_hora = dados.agendado_hora
    if dados.especialista: lead.especialista = dados.especialista
    if dados.notes:        lead.ai_summary = dados.notes

    lead.updated_at = datetime.utcnow()
    db.commit()

    return {"mensagem": "Lead atualizado!", "stage": lead.stage}

@router.get("/retentar")
def leads_para_retentar(db: Session = Depends(get_db)):
    leads = db.query(Lead).filter(
        Lead.stage.in_(["nao_atendeu", "retentar"])
    ).all()

    return [
        {
            "id": l.id,
            "name": l.name,
            "phone": l.phone,
            "stage": l.stage,
            "call_attempts": l.call_attempts,
            "last_call_at": str(l.last_call_at - timedelta(hours=3)) if l.last_call_at else None
        }
        for l in leads
    ]

@router.get("/interessados")
def leads_interessados(db: Session = Depends(get_db)):
    leads = db.query(Lead).filter(
        Lead.stage.in_(["interessado", "agendado"])
    ).order_by(Lead.updated_at.desc()).all()

    return [
        {
            "id": l.id,
            "name": l.name,
            "phone": l.phone,
            "stage": l.stage,
            "product": l.product,
            "resumo": l.resumo,
            "agendado_hora": l.agendado_hora,
            "especialista": l.especialista,
            "call_attempts": l.call_attempts
        }
        for l in leads
    ]