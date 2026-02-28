from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db.database import get_db, Lead
from voice.dialer import fazer_ligacao, gerar_abertura
import os

router = APIRouter()
WEBHOOK_URL = os.getenv("WEBHOOK_BASE_URL", "http://localhost:8000")

class LigarPayload(BaseModel):
    lead_id: Optional[str] = None
    phone:   Optional[str] = None
    name:    Optional[str] = ""

@router.post("/ligar")
def ligar(dados: LigarPayload, db: Session = Depends(get_db)):
    if dados.lead_id:
        lead = db.get(Lead, dados.lead_id)
        if not lead:
            return {"erro": "Lead não encontrado"}
        phone = lead.phone
        name  = lead.name or ""
        lead.stage = "calling"
        lead.call_attempts += 1
        db.commit()
    else:
        phone = dados.phone
        name  = dados.name or ""

    call_sid = fazer_ligacao(phone, WEBHOOK_URL)
    return {"mensagem": "Ligação iniciada!", "call_sid": call_sid, "phone": phone}

@router.post("/atender")
async def atender(To: Optional[str] = Form(None)):
    twiml = gerar_abertura()
    return Response(content=twiml, media_type="application/xml")

@router.post("/responder")
async def responder(
    SpeechResult: Optional[str] = Form(None),
    CallSid:      Optional[str] = Form(None)
):
    fala = SpeechResult or ""
    print(f"🗣️ Cliente disse: {fala}")
    response = VoiceResponse()
    if any(p in fala.lower() for p in ["sim", "pode", "claro", "quero"]):
        response.say(
            "Ótimo! Vou te passar para um dos nossos especialistas. Um momento!",
            voice="Polly.Camila-Neural", language="pt-BR"
        )
    else:
        response.say(
            "Entendo. Se precisar de crédito no futuro, estarei à disposição. Tenha um ótimo dia!",
            voice="Polly.Camila-Neural", language="pt-BR"
        )
    response.hangup()
    return Response(content=str(response), media_type="application/xml")

@router.post("/status")
async def status(
    CallSid:    Optional[str] = Form(None),
    CallStatus: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    print(f"📞 Ligação {CallSid} — Status: {CallStatus}")
    return {"ok": True}