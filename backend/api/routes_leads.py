from fastapi import APIRouter, Depends, HTTPException
from fastapi import UploadFile, File
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db.database import get_db, Lead, CallSession, Meeting, WppMensagem, Callback, normalizar_telefone
from datetime import datetime
import csv, io

router = APIRouter()

class LeadCreate(BaseModel):
    name:  Optional[str] = None
    phone: str
    email: Optional[str] = None
    company: Optional[str] = None

class LeadUpdate(BaseModel):
    name:  Optional[str] = None
    phone: Optional[str] = None
    wpp_phone: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    stage: Optional[str] = None


@router.post("/")
def criar_lead(dados: LeadCreate, db: Session = Depends(get_db)):
    # ✅ Normaliza telefone ANTES de salvar
    phone_norm = normalizar_telefone(dados.phone)

    # ✅ Verifica se já existe
    existente = db.query(Lead).filter(Lead.phone.contains(phone_norm[-8:])).first()
    if existente:
        return {"erro": f"Lead já existe com esse número: {existente.name or 'sem nome'}", "id": existente.id}

    lead = Lead(name=dados.name, phone=phone_norm, email=dados.email, company=dados.company or "")
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"mensagem": "Lead cadastrado!", "id": lead.id}


@router.get("/")
def listar_leads(db: Session = Depends(get_db)):
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
    return [
        {
            "id"          : l.id,
            "name"        : l.name,
            "phone"       : l.phone,
            "wpp_phone"   : l.wpp_phone or "",
            "email"       : l.email,
            "company"     : getattr(l, 'company', '') or '',
            "stage"       : l.stage,
            "temperature" : l.temperature,
            "product"     : l.product,
            "resumo"      : l.resumo,
            "conversa"    : l.conversa,
            "agendado_hora": l.agendado_hora,
            "scheduled_at": l.scheduled_at,
            "especialista": l.especialista,
            "call_attempts": l.call_attempts,
            "last_call_at": str(l.last_call_at) if l.last_call_at else None,
            "ia_pausada"  : getattr(l, 'ia_pausada', False) or False,
            "created_at"  : str(l.created_at)
        }
        for l in leads
    ]


@router.get("/{lead_id}")
def buscar_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    return lead


@router.put("/{lead_id}")
def atualizar_lead(lead_id: str, dados: LeadUpdate, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    if dados.name  is not None: lead.name  = dados.name
    if dados.phone is not None: lead.phone = normalizar_telefone(dados.phone)
    if dados.wpp_phone is not None: lead.wpp_phone = normalizar_telefone(dados.wpp_phone) if dados.wpp_phone.strip() else ""
    if dados.email is not None: lead.email = dados.email
    if dados.company is not None: lead.company = dados.company
    if dados.stage is not None: lead.stage = dados.stage
    lead.updated_at = datetime.utcnow()
    db.commit()
    return {"mensagem": "Lead atualizado!", "stage": lead.stage}


@router.delete("/{lead_id}")
def deletar_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    # Deleta filhos primeiro
    db.query(WppMensagem).filter(WppMensagem.lead_id == lead_id).delete()
    db.query(CallSession).filter(CallSession.lead_id == lead_id).delete()
    db.query(Meeting).filter(Meeting.lead_id == lead_id).delete()
    db.query(Callback).filter(Callback.lead_id == lead_id).delete()

    db.delete(lead)
    db.commit()
    return {"mensagem": "Lead deletado!"}


@router.post("/importar-csv")
async def importar_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    conteudo = await file.read()

    # Auto-detect encoding
    texto = None
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            texto = conteudo.decode(enc)
            # Verifica se decodificou sem lixo
            if "ï¿½" not in texto:
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if not texto:
        texto = conteudo.decode("latin-1")

    # Auto-detect separador
    primeira_linha = texto.split("\n")[0] if texto else ""
    separador = ";" if primeira_linha.count(";") > primeira_linha.count(",") else ","

    reader = csv.DictReader(io.StringIO(texto), delimiter=separador)

    importados = 0
    duplicados = 0
    erros = 0
    for row in reader:
        # Normaliza chaves (lowercase, sem espaços extras, sem acentos problemáticos)
        r = {}
        for k, v in row.items():
            if k:
                key = k.strip().lower()
                # Normaliza acentos comuns em nomes de coluna
                key = key.replace("ã", "a").replace("á", "a").replace("é", "e")
                key = key.replace("í", "i").replace("ó", "o").replace("ú", "u")
                key = key.replace("ç", "c").replace("õ", "o")
                r[key] = v.strip() if v else ""

        # Busca telefone — tenta várias colunas
        phone = ""
        for col in r:
            if any(t in col for t in ["telefone", "phone", "tel", "celular", "fone"]) and r[col]:
                phone = r[col]
                break

        if not phone or len(phone.strip()) < 8:
            erros += 1
            continue

        # Telefone precisa ser numérico (ignora emails no campo errado)
        phone_digits = "".join(c for c in phone if c.isdigit())
        if len(phone_digits) < 8:
            erros += 1
            continue

        # Busca NOME DO SÓCIO (nome da pessoa)
        name = ""
        for col in r:
            if any(t in col for t in ["nome do s", "nome do socio", "nome"]) and "empresa" not in col and r[col]:
                name = r[col]
                break
        
        # Se nome tem vários sócios separados por "-", pega o primeiro
        if name and "-" in name:
            name = name.split("-")[0].strip()
        
        # Limpa números do início do nome
        if name:
            import re
            name = re.sub(r'^[\d./\-\s]+', '', name).strip()
            if name:
                name = name.title()

        # Busca RAZÃO SOCIAL (nome da empresa)
        company = ""
        for col in r:
            if any(t in col for t in ["raz", "empresa", "razao", "company"]) and r[col]:
                company = r[col]
                break
        
        # Limpa números do início da razão social
        if company:
            company = re.sub(r'^[\d./\-\s]+', '', company).strip()
            if company:
                company = company.title()

        # Se não achou nome do sócio, usa razão social como fallback
        if not name and company:
            name = company

        # Busca email
        email = ""
        for col in r:
            if any(t in col for t in ["email", "e-mail", "e_mail"]) and r[col]:
                email = r[col]
                break

        # Normaliza telefone
        phone_norm = normalizar_telefone(phone.strip())

        # Verifica duplicata
        existente = db.query(Lead).filter(Lead.phone.contains(phone_norm[-8:])).first()
        if existente:
            duplicados += 1
            continue

        lead = Lead(name=name.strip(), phone=phone_norm, email=email.strip(), company=company.strip())
        db.add(lead)
        importados += 1

    db.commit()
    return {
        "mensagem": f"{importados} leads importados! ({duplicados} duplicados, {erros} sem telefone)",
        "importados": importados,
        "duplicados": duplicados,
        "erros": erros,
    }


# ── PAUSAR / ATIVAR IA ───────────────────────────────────────────────────────

@router.post("/{lead_id}/pausar-ia")
def toggle_pausar_ia(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}
    lead.ia_pausada = not lead.ia_pausada
    db.commit()
    status = "pausada" if lead.ia_pausada else "ativada"
    print(f"{'⏸️' if lead.ia_pausada else '▶️'} IA {status} para {lead.name} ({lead.phone})")
    return {"mensagem": f"IA {status}", "ia_pausada": lead.ia_pausada}