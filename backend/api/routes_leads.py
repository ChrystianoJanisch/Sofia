from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Form
from fastapi import UploadFile, File
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from db.database import get_db, Lead, CallSession, Meeting, WppMensagem, Callback, Transferencia, normalizar_telefone
from datetime import datetime, timedelta
import csv, io, os, asyncio, uuid

router = APIRouter()

class LeadCreate(BaseModel):
    name:  Optional[str] = None
    phone: str
    email: Optional[str] = None
    company: Optional[str] = None
    cnpj: Optional[str] = None

class LeadUpdate(BaseModel):
    name:  Optional[str] = None
    phone: Optional[str] = None
    wpp_phone: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    cnpj: Optional[str] = None
    stage: Optional[str] = None


@router.post("/")
def criar_lead(dados: LeadCreate, db: Session = Depends(get_db)):
    # ✅ Normaliza telefone ANTES de salvar
    phone_norm = normalizar_telefone(dados.phone)

    # ✅ Verifica se já existe
    existente = db.query(Lead).filter(Lead.phone.contains(phone_norm[-8:])).first()
    if existente:
        return {"erro": f"Lead já existe com esse número: {existente.name or 'sem nome'}", "id": existente.id}

    lead = Lead(name=dados.name, phone=phone_norm, email=dados.email, company=dados.company or "", cnpj=dados.cnpj or "")
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
            "cnpj"        : getattr(l, 'cnpj', '') or '',
            "stage"       : l.stage,
            "temperature" : l.temperature,
            "product"     : l.product,
            "resumo"      : l.resumo,
            "conversa"    : l.conversa,
            "agendado_hora": l.agendado_hora,
            "scheduled_at": l.scheduled_at,
            "especialista": l.especialista,
            "call_attempts": l.call_attempts,
            "last_call_at": str(l.last_call_at - timedelta(hours=3)) if l.last_call_at else None,
            "ia_pausada"  : getattr(l, 'ia_pausada', False) or False,
            "parceira_indicada": getattr(l, 'parceira_indicada', '') or '',
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
    if dados.cnpj is not None: lead.cnpj = dados.cnpj
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
    db.query(Transferencia).filter(Transferencia.lead_id == lead_id).delete()
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


# ── BROADCAST WHATSAPP ───────────────────────────────────────────────────────

_broadcast_estado = {
    "ativo": False,
    "total": 0,
    "enviados": 0,
    "erros": 0,
    "status": "parado",
}


@router.post("/broadcast")
async def broadcast_imagem(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    caption: str = Form(""),
    lead_ids: str = Form(""),
    db: Session = Depends(get_db)
):
    if _broadcast_estado["ativo"]:
        return {"erro": "Já existe um broadcast rodando!"}

    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "jpg"
    nome_arquivo = f"broadcast_{uuid.uuid4().hex[:8]}.{ext}"
    pasta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "img")
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, nome_arquivo)

    conteudo = await file.read()
    with open(caminho, "wb") as f:
        f.write(conteudo)

    base_url = os.getenv("WEBHOOK_BASE_URL", "https://sofia-ai-production.up.railway.app")
    url_imagem = f"{base_url}/static/img/{nome_arquivo}"

    query = db.query(Lead).filter(Lead.phone != None, Lead.phone != "", Lead.stage != "novo")
    if lead_ids:
        ids_list = [i.strip() for i in lead_ids.split(",") if i.strip()]
        if ids_list:
            query = query.filter(Lead.id.in_(ids_list))
    leads_found = query.all()
    telefones = list(set(l.phone for l in leads_found if l.phone and len(l.phone) >= 8))

    if not telefones:
        return {"erro": "Nenhum lead com telefone encontrado"}

    _broadcast_estado.update({
        "ativo": True, "total": len(telefones),
        "enviados": 0, "erros": 0, "status": "rodando"
    })

    background_tasks.add_task(_executar_broadcast, telefones, url_imagem, caption)

    return {
        "mensagem": f"Broadcast iniciado para {len(telefones)} leads!",
        "total": len(telefones),
        "imagem": url_imagem,
    }


@router.get("/broadcast/status")
async def broadcast_status():
    return {
        "ativo": _broadcast_estado["ativo"],
        "status": _broadcast_estado["status"],
        "total": _broadcast_estado["total"],
        "enviados": _broadcast_estado["enviados"],
        "erros": _broadcast_estado["erros"],
        "restantes": _broadcast_estado["total"] - _broadcast_estado["enviados"] - _broadcast_estado["erros"],
    }


@router.post("/broadcast/cancelar")
async def broadcast_cancelar():
    if not _broadcast_estado["ativo"]:
        return {"mensagem": "Nenhum broadcast ativo"}
    _broadcast_estado["status"] = "cancelado"
    return {"mensagem": "Broadcast cancelado!"}


async def _executar_broadcast(telefones: list, url_imagem: str, caption: str):
    from integrations.whatsapp import enviar_imagem
    try:
        for i, tel in enumerate(telefones):
            if _broadcast_estado["status"] == "cancelado":
                print(f"🛑 Broadcast cancelado. {i}/{len(telefones)}")
                break
            try:
                enviar_imagem(tel, url_imagem, caption=caption)
                _broadcast_estado["enviados"] += 1
                print(f"📤 Broadcast [{i+1}/{len(telefones)}] — {tel}")
            except Exception as e:
                _broadcast_estado["erros"] += 1
                print(f"❌ Broadcast erro {tel}: {e}")
            if i < len(telefones) - 1:
                await asyncio.sleep(2)
    finally:
        _broadcast_estado["ativo"] = False
        _broadcast_estado["status"] = "parado"
        total = _broadcast_estado["enviados"]
        erros = _broadcast_estado["erros"]
        print(f"✅ Broadcast finalizado: {total} enviados, {erros} erros")


# ── 1ª MENSAGEM WHATSAPP (Template em massa pros leads NOVO) ─────────────────

_first_msg_estado = {
    "ativo": False,
    "total": 0,
    "enviados": 0,
    "erros": 0,
    "status": "parado",
    "template": "",
}


class FirstMessagePayload(BaseModel):
    template_name: str
    language_code: Optional[str] = "pt_BR"
    variable_mapping: dict = {}  # { "1": "name" | "company" | "custom:Texto" }
    lead_ids: Optional[list] = None  # Se vazio, envia pra todos NOVO
    delay_segundos: Optional[int] = 3


@router.post("/first-message")
async def first_message(
    dados: FirstMessagePayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if _first_msg_estado["ativo"]:
        return {"erro": "Já existe um envio em lote rodando!"}

    if not dados.template_name:
        return {"erro": "template_name é obrigatório"}

    # Busca leads elegíveis
    if dados.lead_ids:
        leads = db.query(Lead).filter(
            Lead.id.in_(dados.lead_ids),
            Lead.phone != None, Lead.phone != "",
        ).all()
    else:
        leads = db.query(Lead).filter(
            Lead.stage == "novo",
            Lead.phone != None, Lead.phone != "",
        ).all()

    # Filtra leads com telefone válido
    leads_validos = [l for l in leads if l.phone and len(l.phone) >= 8]

    if not leads_validos:
        return {"erro": "Nenhum lead elegível encontrado"}

    _first_msg_estado.update({
        "ativo": True,
        "total": len(leads_validos),
        "enviados": 0,
        "erros": 0,
        "status": "rodando",
        "template": dados.template_name,
    })

    lead_ids = [l.id for l in leads_validos]
    background_tasks.add_task(
        _executar_first_message,
        lead_ids,
        dados.template_name,
        dados.language_code or "pt_BR",
        dados.variable_mapping or {},
        dados.delay_segundos or 3,
    )

    return {
        "mensagem": f"Envio iniciado para {len(leads_validos)} leads!",
        "total": len(leads_validos),
        "template": dados.template_name,
    }


@router.get("/first-message/status")
async def first_message_status():
    return {
        "ativo": _first_msg_estado["ativo"],
        "status": _first_msg_estado["status"],
        "total": _first_msg_estado["total"],
        "enviados": _first_msg_estado["enviados"],
        "erros": _first_msg_estado["erros"],
        "template": _first_msg_estado["template"],
        "restantes": _first_msg_estado["total"] - _first_msg_estado["enviados"] - _first_msg_estado["erros"],
    }


@router.post("/first-message/cancelar")
async def first_message_cancelar():
    if not _first_msg_estado["ativo"]:
        return {"mensagem": "Nenhum envio ativo"}
    _first_msg_estado["status"] = "cancelado"
    return {"mensagem": "Envio cancelado!"}


def _resolver_variavel(lead, source: str) -> str:
    """Resolve o valor de uma variável a partir do mapeamento."""
    if source.startswith("custom:"):
        return source[7:]
    # Campos diretos do lead
    mapa = {
        "name": lead.name or "",
        "company": lead.company or "",
        "phone": lead.phone or "",
        "cnpj": lead.cnpj or "",
        "email": lead.email or "",
        "product": lead.product or "",
    }
    return mapa.get(source, "") or " "


async def _executar_first_message(
    lead_ids: list,
    template_name: str,
    language_code: str,
    variable_mapping: dict,
    delay_segundos: int,
):
    from db.database import SessionLocal
    from integrations.whatsapp import _enviar_template, _formatar_numero
    db = SessionLocal()
    try:
        for i, lead_id in enumerate(lead_ids):
            if _first_msg_estado["status"] == "cancelado":
                print(f"🛑 First-message cancelado. {i}/{len(lead_ids)}")
                break

            lead = db.get(Lead, lead_id)
            if not lead or not lead.phone:
                _first_msg_estado["erros"] += 1
                continue

            # Monta parâmetros na ordem das variáveis
            keys_ordenadas = sorted(variable_mapping.keys(), key=lambda k: int(k))
            params = [_resolver_variavel(lead, variable_mapping[k]) for k in keys_ordenadas]

            numero = _formatar_numero(lead.phone)
            try:
                wamid = _enviar_template(numero, template_name, params, lang=language_code)
                if wamid:
                    # Sucesso: muda stage e salva msg no inbox
                    lead.stage = "mensagem_enviada"
                    lead.updated_at = datetime.utcnow()

                    # Substitui {{N}} no body_text pro inbox (se tivermos)
                    # Simples: só salva indicador que template foi enviado
                    inbox_msg = f"[Template enviado: {template_name}] " + " | ".join(params)
                    wpp_msg = WppMensagem(
                        lead_id=lead.id,
                        role="assistant",
                        content=inbox_msg,
                        wamid=wamid,
                        status="sent",
                    )
                    db.add(wpp_msg)
                    db.commit()
                    _first_msg_estado["enviados"] += 1
                    print(f"📤 First-msg [{i+1}/{len(lead_ids)}] — {lead.name or numero}")
                else:
                    _first_msg_estado["erros"] += 1
                    print(f"❌ First-msg falhou {numero}")
            except Exception as e:
                _first_msg_estado["erros"] += 1
                print(f"❌ First-msg erro {numero}: {e}")

            if i < len(lead_ids) - 1:
                await asyncio.sleep(max(delay_segundos, 2))
    finally:
        _first_msg_estado["ativo"] = False
        _first_msg_estado["status"] = "parado"
        total = _first_msg_estado["enviados"]
        erros = _first_msg_estado["erros"]
        print(f"✅ First-message finalizado: {total} enviados, {erros} erros")
        db.close()