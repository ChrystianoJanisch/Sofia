"""
Rotas de autenticação e gestão de usuários.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from db.database import get_db, User
from api.auth import (
    hash_password, verify_password, create_token,
    get_current_user, require_master,
)
from datetime import datetime

router = APIRouter()


# ── SCHEMAS ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class CreateUserRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    role: str = "funcionario"  # "master" ou "funcionario"


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None


# ── LOGIN ─────────────────────────────────────────────────────────────────────

@router.post("/login")
def login(dados: LoginRequest, db: Session = Depends(get_db)):
    """Autenticação — retorna JWT token."""
    user = db.query(User).filter(User.email == dados.email.lower().strip()).first()

    if not user or not verify_password(dados.password, user.password_hash):
        raise HTTPException(status_code=401, detail="E-mail ou senha inválidos")

    if not user.active:
        raise HTTPException(status_code=403, detail="Usuário desativado")

    token = create_token(user.id, user.email, user.role, user.name)

    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        }
    }


# ── QUEM SOU EU ───────────────────────────────────────────────────────────────

@router.get("/me")
def me(user: User = Depends(get_current_user)):
    """Retorna dados do usuário autenticado."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "active": user.active,
    }


# ── LISTAR USUÁRIOS (master only) ────────────────────────────────────────────

@router.get("/users")
def listar_usuarios(master: User = Depends(require_master), db: Session = Depends(get_db)):
    """Lista todos os usuários — apenas master."""
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "role": u.role,
            "active": u.active,
            "created_at": u.created_at.isoformat() if u.created_at else "",
        }
        for u in users
    ]


# ── CRIAR USUÁRIO (master only) ──────────────────────────────────────────────

@router.post("/users")
def criar_usuario(dados: CreateUserRequest, master: User = Depends(require_master), db: Session = Depends(get_db)):
    """Cria novo usuário — apenas master."""
    email = dados.email.lower().strip()

    # Verifica se email já existe
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=400, detail="E-mail já cadastrado")

    # Validação do role
    if dados.role not in ("master", "funcionario"):
        raise HTTPException(status_code=400, detail="Role inválido. Use 'master' ou 'funcionario'")

    user = User(
        email=email,
        password_hash=hash_password(dados.password),
        name=dados.name,
        role=dados.role,
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "ok": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        }
    }


# ── EDITAR USUÁRIO (master only) ─────────────────────────────────────────────

@router.put("/users/{user_id}")
def editar_usuario(user_id: str, dados: UpdateUserRequest, master: User = Depends(require_master), db: Session = Depends(get_db)):
    """Edita um usuário — apenas master."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    if dados.name is not None:
        user.name = dados.name
    if dados.email is not None:
        user.email = dados.email.lower().strip()
    if dados.password is not None and dados.password:
        user.password_hash = hash_password(dados.password)
    if dados.role is not None:
        if dados.role not in ("master", "funcionario"):
            raise HTTPException(status_code=400, detail="Role inválido")
        user.role = dados.role
    if dados.active is not None:
        user.active = dados.active

    user.updated_at = datetime.utcnow()
    db.commit()

    return {"ok": True}


# ── DELETAR USUÁRIO (master only) ────────────────────────────────────────────

@router.delete("/users/{user_id}")
def deletar_usuario(user_id: str, master: User = Depends(require_master), db: Session = Depends(get_db)):
    """Deleta um usuário permanentemente — apenas master."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    if user.id == master.id:
        raise HTTPException(status_code=400, detail="Você não pode deletar a si mesmo")

    # Remove especialista vinculado (se houver)
    from db.database import Especialista, Transferencia
    esp = db.query(Especialista).filter(Especialista.email == user.email).first()
    if esp:
        db.query(Transferencia).filter(Transferencia.especialista_id == esp.id).delete()
        db.delete(esp)

    db.delete(user)
    db.commit()

    return {"ok": True, "mensagem": "Usuário deletado permanentemente"}


# ── ESQUECI MINHA SENHA ───────────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: str

@router.post("/forgot-password")
def forgot_password(dados: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Solicita redefinição de senha — gera senha temporária."""
    email = dados.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"erro": "E-mail não encontrado no sistema"}

    # Gera senha temporária
    import random, string
    temp_pass = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    user.password_hash = hash_password(temp_pass)
    user.updated_at = datetime.utcnow()
    db.commit()

    # Tenta notificar admin via WhatsApp
    try:
        import os
        from integrations.whatsapp import _enviar
        admin_phone = os.getenv("ADMIN_WHATSAPP", "")
        if admin_phone:
            _enviar(admin_phone,
                f"🔑 Senha redefinida!\n\n"
                f"Usuário: {user.name} ({user.email})\n"
                f"Nova senha temporária: {temp_pass}\n\n"
                f"Passe essa senha pro usuário e peça pra trocar depois.")
    except:
        pass

    print(f"🔑 Senha temporária gerada para {user.email}: {temp_pass}")
    return {"mensagem": "Uma nova senha temporária foi gerada. Entre em contato com o administrador para recebê-la."}


# ── RESET SENHA (master) ─────────────────────────────────────────────────────

class ResetPasswordRequest(BaseModel):
    user_id: str
    new_password: str

@router.post("/reset-password")
def reset_password(dados: ResetPasswordRequest, master: User = Depends(require_master), db: Session = Depends(get_db)):
    """Master reseta a senha de um usuário."""
    user = db.get(User, dados.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    user.password_hash = hash_password(dados.new_password)
    user.updated_at = datetime.utcnow()
    db.commit()

    return {"ok": True, "mensagem": f"Senha de {user.name} redefinida com sucesso"}