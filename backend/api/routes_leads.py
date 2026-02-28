from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from db.database import get_db, Lead
from datetime import datetime

router = APIRouter()

# Formato dos dados que chegam na API
class LeadCreate(BaseModel):
    name:  Optional[str] = None
    phone: str
    email: Optional[str] = None

# ── POST /api/leads — Cadastrar novo lead ──────────────
@router.post("/")
def criar_lead(dados: LeadCreate, db: Session = Depends(get_db)):
    lead = Lead(
        name=dados.name,
        phone=dados.phone,
        email=dados.email
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"mensagem": "Lead cadastrado!", "id": lead.id, "phone": lead.phone}

# ── GET /api/leads — Listar todos os leads ─────────────
@router.get("/")
def listar_leads(db: Session = Depends(get_db)):
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
    return {
        "total": len(leads),
        "leads": [{
            "id"         : l.id,
            "name"       : l.name,
            "phone"      : l.phone,
            "stage"      : l.stage,
            "temperature": l.temperature,
            "product"    : l.product,
            "created_at" : str(l.created_at)
        } for l in leads]
    }

# ── GET /api/leads/{id} — Buscar um lead ───────────────
@router.get("/{lead_id}")
def buscar_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    return lead