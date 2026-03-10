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
    """Desativa um usuário — apenas master. Não deleta do banco."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    if user.id == master.id:
        raise HTTPException(status_code=400, detail="Você não pode desativar a si mesmo")

    user.active = False
    user.updated_at = datetime.utcnow()
    db.commit()

    return {"ok": True}