"""
Rotas de Especialistas — CRUD + Transferência + Painel
"""

import json, os
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from db.database import (
    get_db, Lead, Especialista, Transferencia, WppMensagem
)

router = APIRouter()
BRT = timezone(timedelta(hours=-3))


# ── Schemas ───────────────────────────────────────────────────────────────────

class EspecialistaCreate(BaseModel):
    nome: str
    area: str = ""
    titulo: str = ""
    whatsapp: str = ""
    email: str = ""
    senha: str = ""

class EspecialistaUpdate(BaseModel):
    nome: Optional[str] = None
    area: Optional[str] = None
    titulo: Optional[str] = None
    whatsapp: Optional[str] = None
    ativo: Optional[bool] = None

class TransferirPayload(BaseModel):
    lead_id: str
    especialista_id: str
    motivo: str = ""

class MensagemPayload(BaseModel):
    lead_id: str
    texto: str

class EncerrarPayload(BaseModel):
    lead_id: str
    mensagem_final: str = ""

class TransferirEntrePayload(BaseModel):
    lead_id: str
    novo_especialista_id: str
    motivo: str = ""


# ── CRUD Especialistas ────────────────────────────────────────────────────────

@router.get("/")
def listar_especialistas(db: Session = Depends(get_db)):
    esps = db.query(Especialista).order_by(Especialista.nome).all()
    return [
        {
            "id": e.id,
            "nome": e.nome,
            "area": e.area,
            "titulo": e.titulo,
            "whatsapp": e.whatsapp,
            "email": e.email,
            "ativo": e.ativo,
            "online": e.online,
            "atendimentos_ativos": e.atendimentos_ativos,
            "created_at": str(e.created_at),
        }
        for e in esps
    ]


@router.post("/")
def criar_especialista(dados: EspecialistaCreate, db: Session = Depends(get_db)):
    from passlib.hash import bcrypt
    esp = Especialista(
        nome=dados.nome,
        area=dados.area,
        titulo=dados.titulo,
        whatsapp=dados.whatsapp,
        email=dados.email,
        senha_hash=bcrypt.hash(dados.senha) if dados.senha else "",
    )
    db.add(esp)
    db.commit()
    return {"mensagem": "Especialista criado", "id": esp.id}


@router.put("/{esp_id}")
def atualizar_especialista(esp_id: str, dados: EspecialistaUpdate, db: Session = Depends(get_db)):
    esp = db.get(Especialista, esp_id)
    if not esp:
        raise HTTPException(status_code=404, detail="Especialista não encontrado")
    if dados.nome is not None: esp.nome = dados.nome
    if dados.area is not None: esp.area = dados.area
    if dados.titulo is not None: esp.titulo = dados.titulo
    if dados.whatsapp is not None: esp.whatsapp = dados.whatsapp
    if dados.ativo is not None: esp.ativo = dados.ativo
    db.commit()
    return {"mensagem": "Atualizado"}


@router.delete("/{esp_id}")
def deletar_especialista(esp_id: str, db: Session = Depends(get_db)):
    esp = db.get(Especialista, esp_id)
    if not esp:
        raise HTTPException(status_code=404, detail="Especialista não encontrado")
    # Remove transferências ativas
    db.query(Transferencia).filter(Transferencia.especialista_id == esp_id).delete()
    db.delete(esp)
    db.commit()
    return {"mensagem": "Especialista removido"}


# ── TRANSFERÊNCIA ─────────────────────────────────────────────────────────────

@router.post("/transferir")
def transferir_lead(dados: TransferirPayload, db: Session = Depends(get_db)):
    """Transfere um lead pra um especialista — pausa IA + notifica"""
    lead = db.get(Lead, dados.lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    esp = db.get(Especialista, dados.especialista_id)
    if not esp:
        return {"erro": "Especialista não encontrado"}

    # Pausa IA
    lead.ia_pausada = True
    lead.especialista_id = esp.id

    # Monta contexto pro especialista
    contexto = (
        f"Nome: {lead.name or '—'}\n"
        f"Telefone: {lead.phone}\n"
        f"Empresa: {getattr(lead, 'company', '') or '—'}\n"
        f"Produto: {lead.product or '—'}\n"
        f"Valor: {lead.desired_value or '—'}\n"
        f"Temperatura: {lead.temperature or '—'}\n"
        f"Resumo: {lead.resumo or '—'}\n"
    )

    # Cria transferência
    transf = Transferencia(
        lead_id=lead.id,
        especialista_id=esp.id,
        motivo=dados.motivo or "Transferência manual",
        contexto=contexto,
    )
    db.add(transf)

    # Incrementa atendimentos ativos
    esp.atendimentos_ativos = (esp.atendimentos_ativos or 0) + 1

    db.commit()

    # Avisa cliente no WhatsApp
    from integrations.whatsapp import _enviar
    numero_wpp = lead.wpp_phone or lead.phone or ""
    titulo_esp = esp.titulo or "Especialista"
    msg_transferencia = (
        f"Vou te passar para o profissional mais indicado pra te ajudar. "
        f"Ele já tem todo o contexto da nossa conversa, aguarda só um momentinho! 😊"
    )
    _enviar(numero_wpp, msg_transferencia)

    # Salva no histórico
    wpp_msg = WppMensagem(lead_id=lead.id, role="assistant", content=msg_transferencia)
    db.add(wpp_msg)
    db.commit()

    # Notifica especialista no WhatsApp pessoal
    if esp.whatsapp:
        try:
            notif = (
                f"🔔 Novo atendimento!\n\n"
                f"Cliente: {lead.name or '—'}\n"
                f"Telefone: {lead.phone}\n"
                f"Produto: {lead.product or '—'}\n"
                f"Resumo: {lead.resumo or '—'}\n\n"
                f"Acesse o painel pra responder."
            )
            _enviar(esp.whatsapp, notif)
        except Exception as e:
            print(f"⚠️ Erro ao notificar especialista: {e}")

    print(f"🔄 Lead {lead.name} transferido para {esp.nome} ({esp.titulo})")
    return {"mensagem": "Transferido!", "transferencia_id": transf.id}


@router.post("/enviar-mensagem")
def enviar_mensagem_especialista(dados: MensagemPayload, db: Session = Depends(get_db)):
    """Especialista envia mensagem pro cliente via WhatsApp da empresa"""
    lead = db.get(Lead, dados.lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    esp = db.get(Especialista, lead.especialista_id) if lead.especialista_id else None
    if not esp:
        return {"erro": "Nenhum especialista vinculado a este lead"}

    # Formata mensagem com identificação do especialista
    msg_formatada = f"*{esp.nome}:*\n{dados.texto}"

    # Envia via WhatsApp da empresa
    from integrations.whatsapp import _enviar
    numero_wpp = lead.wpp_phone or lead.phone or ""
    _enviar(numero_wpp, msg_formatada)

    # Salva no histórico
    wpp_msg = WppMensagem(
        lead_id=lead.id,
        role="assistant",
        content=msg_formatada
    )
    db.add(wpp_msg)

    # Atualiza conversa_estado pra manter contexto
    try:
        estado = json.loads(lead.conversa_estado or "[]")
        if isinstance(estado, dict):
            historico = estado.get("historico", [])
        else:
            historico = estado
        historico.append({"role": "assistant", "content": msg_formatada})
        if isinstance(estado, dict):
            estado["historico"] = historico
            lead.conversa_estado = json.dumps(estado, ensure_ascii=False)
        else:
            lead.conversa_estado = json.dumps(historico, ensure_ascii=False)
    except:
        pass

    db.commit()
    print(f"💬 [{esp.nome}] → {lead.name}: {dados.texto[:60]}")
    return {"mensagem": "Enviada!"}


@router.post("/transferir-entre-especialistas")
def transferir_entre_especialistas(dados: TransferirEntrePayload, db: Session = Depends(get_db)):
    """Especialista transfere atendimento para outro especialista de área diferente"""
    lead = db.get(Lead, dados.lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    novo_esp = db.get(Especialista, dados.novo_especialista_id)
    if not novo_esp:
        return {"erro": "Especialista destino não encontrado"}

    # Pega especialista atual
    antigo_esp_id = lead.especialista_id
    antigo_esp = db.get(Especialista, antigo_esp_id) if antigo_esp_id else None

    # Encerra transferência antiga
    transf_antiga = (
        db.query(Transferencia)
        .filter(Transferencia.lead_id == lead.id, Transferencia.status == "ativa")
        .first()
    )
    if transf_antiga:
        transf_antiga.status = "encerrada"
        transf_antiga.encerrada_em = datetime.utcnow()

    # Decrementa atendimentos do especialista antigo
    if antigo_esp:
        antigo_esp.atendimentos_ativos = max(0, (antigo_esp.atendimentos_ativos or 0) - 1)

    # Atribui novo especialista
    lead.especialista_id = novo_esp.id
    # lead.ia_pausada continua True

    # Monta contexto
    contexto = (
        f"Nome: {lead.name or '—'}\n"
        f"Telefone: {lead.phone}\n"
        f"Empresa: {getattr(lead, 'company', '') or '—'}\n"
        f"Produto: {lead.product or '—'}\n"
        f"Resumo: {lead.resumo or '—'}\n"
        f"Transferido de: {antigo_esp.nome if antigo_esp else '—'}\n"
        f"Motivo: {dados.motivo}\n"
    )

    # Cria nova transferência
    transf = Transferencia(
        lead_id=lead.id,
        especialista_id=novo_esp.id,
        motivo=dados.motivo or f"Transferido de {antigo_esp.nome if antigo_esp else '—'}",
        contexto=contexto,
    )
    db.add(transf)
    novo_esp.atendimentos_ativos = (novo_esp.atendimentos_ativos or 0) + 1

    # Avisa cliente no WhatsApp
    from integrations.whatsapp import _enviar
    numero_wpp = lead.wpp_phone or lead.phone or ""
    titulo_novo = novo_esp.titulo or "Especialista"
    nome_antigo = antigo_esp.nome if antigo_esp else "nosso atendente"
    msg = (
        f"Seu atendimento foi transferido para outro profissional mais indicado pra te ajudar com essa questão. "
        f"Ele já tem todo o contexto da nossa conversa! 😊"
    )
    _enviar(numero_wpp, msg)

    # Salva no histórico
    wpp_msg = WppMensagem(lead_id=lead.id, role="assistant", content=msg)
    db.add(wpp_msg)

    # Notifica novo especialista no WhatsApp pessoal
    if novo_esp.whatsapp:
        try:
            import os
            notif = (
                f"🔄 Atendimento transferido!\n\n"
                f"De: {nome_antigo}\n"
                f"Cliente: {lead.name or '—'}\n"
                f"Telefone: {lead.phone}\n"
                f"Motivo: {dados.motivo or '—'}\n\n"
                f"Acesse o painel pra responder:\n"
                f"{os.getenv('WEBHOOK_BASE_URL', '')}/painel-especialista"
            )
            _enviar(novo_esp.whatsapp, notif)
        except Exception as e:
            print(f"⚠️ Erro ao notificar novo especialista: {e}")

    db.commit()
    print(f"🔄 Lead {lead.name} transferido de {nome_antigo} para {novo_esp.nome}")
    return {"mensagem": f"Transferido para {novo_esp.nome}!"}


@router.post("/encerrar")
def encerrar_atendimento(dados: EncerrarPayload, db: Session = Depends(get_db)):
    """Especialista encerra atendimento — Julia volta a responder"""
    lead = db.get(Lead, dados.lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    esp_id = lead.especialista_id
    esp = db.get(Especialista, esp_id) if esp_id else None

    # Mensagem de despedida se tiver
    if dados.mensagem_final:
        from integrations.whatsapp import _enviar
        numero_wpp = lead.wpp_phone or lead.phone or ""
        msg = f"*{esp.nome if esp else 'Especialista'}:*\n{dados.mensagem_final}"
        _enviar(numero_wpp, msg)
        wpp_msg = WppMensagem(lead_id=lead.id, role="assistant", content=msg)
        db.add(wpp_msg)

    # Reativa Julia
    lead.ia_pausada = False
    lead.especialista_id = ""

    # Encerra transferência ativa
    transf = (
        db.query(Transferencia)
        .filter(Transferencia.lead_id == lead.id, Transferencia.status == "ativa")
        .first()
    )
    if transf:
        transf.status = "encerrada"
        transf.encerrada_em = datetime.utcnow()

    # Decrementa atendimentos
    if esp:
        esp.atendimentos_ativos = max(0, (esp.atendimentos_ativos or 0) - 1)

    db.commit()
    print(f"✅ Atendimento encerrado: {lead.name} — Julia reativada")
    return {"mensagem": "Atendimento encerrado, Julia reativada"}


# ── PAINEL DO ESPECIALISTA ────────────────────────────────────────────────────

@router.get("/meus-atendimentos/{esp_id}")
def meus_atendimentos(esp_id: str, db: Session = Depends(get_db)):
    """Retorna leads que estão sendo atendidos por um especialista"""
    leads = (
        db.query(Lead)
        .filter(Lead.especialista_id == esp_id, Lead.ia_pausada == True)
        .all()
    )
    resultado = []
    for l in leads:
        # Pega últimas mensagens
        msgs = (
            db.query(WppMensagem)
            .filter(WppMensagem.lead_id == l.id)
            .order_by(WppMensagem.created_at.desc())
            .limit(50)
            .all()
        )
        msgs.reverse()

        transf = (
            db.query(Transferencia)
            .filter(Transferencia.lead_id == l.id, Transferencia.status == "ativa")
            .first()
        )

        resultado.append({
            "id": l.id,
            "name": l.name,
            "phone": l.phone,
            "company": getattr(l, 'company', '') or '',
            "product": l.product or "",
            "temperature": l.temperature or "",
            "resumo": l.resumo or "",
            "contexto": transf.contexto if transf else "",
            "motivo": transf.motivo if transf else "",
            "iniciada_em": str(transf.iniciada_em) if transf else "",
            "mensagens": [
                {
                    "role": m.role,
                    "content": m.content,
                    "created_at": str(m.created_at),
                }
                for m in msgs
            ]
        })
    return resultado


@router.get("/historico-transferencias")
def historico_transferencias(db: Session = Depends(get_db)):
    """Histórico de todas as transferências"""
    transfs = (
        db.query(Transferencia)
        .order_by(Transferencia.iniciada_em.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": t.id,
            "lead_name": t.lead.name if t.lead else "—",
            "especialista_nome": t.especialista.nome if t.especialista else "—",
            "motivo": t.motivo,
            "status": t.status,
            "iniciada_em": str(t.iniciada_em),
            "encerrada_em": str(t.encerrada_em) if t.encerrada_em else "",
        }
        for t in transfs
    ]