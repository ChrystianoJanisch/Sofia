"""
Módulo de autenticação — JWT + bcrypt + dependências FastAPI.
"""
import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from db.database import get_db, User

# ── CONFIG ────────────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("JWT_SECRET_KEY", os.getenv("ELEVENLABS_API_KEY", "flcbank-secret-key-change-me"))
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)


# ── PASSWORD ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password[:72])


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain[:72], hashed)


# ── JWT TOKEN ─────────────────────────────────────────────────────────────────

def create_token(user_id: str, email: str, role: str, name: str = "") -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "name": name,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ── DEPENDENCIES ──────────────────────────────────────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Dependência que retorna o usuário autenticado ou 401."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Token não fornecido")

    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")

    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Usuário não encontrado ou desativado")

    return user


async def require_master(user: User = Depends(get_current_user)) -> User:
    """Dependência que exige role=master."""
    if user.role != "master":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    return user


# ── OPTIONAL AUTH (para rotas que funcionam com ou sem auth) ──────────────────

async def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Retorna o usuário se autenticado, None se não."""
    if not credentials:
        return None
    payload = decode_token(credentials.credentials)
    if not payload:
        return None
    user = db.query(User).filter(User.id == payload.get("sub")).first()
    if not user or not user.active:
        return None
    return user