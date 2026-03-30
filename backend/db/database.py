from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Float, Boolean, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import uuid, os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sofia.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def normalizar_telefone(phone: str) -> str:
    """
    Normaliza telefone para APENAS dígitos com DDI 55.
    Ex: '(51) 99746-4857' → '5551997464857'
    
    USAR SEMPRE ao salvar lead.phone.
    Isso garante que .contains() funcione na busca do WhatsApp.
    """
    digits = "".join(c for c in phone if c.isdigit())
    if not digits.startswith("55") and len(digits) <= 11:
        digits = "55" + digits
    # Adiciona 9 se falta (formato: 55 + DDD(2) + 9 + numero(8) = 13)
    if len(digits) == 12:
        digits = digits[:4] + "9" + digits[4:]
    return digits


class Lead(Base):
    __tablename__ = "leads"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name          = Column(String)
    phone         = Column(String, nullable=False)
    email         = Column(String, default="")
    company       = Column(String, default="")

    stage         = Column(String, default="novo")

    temperature   = Column(String, default="pending")
    score         = Column(Float, default=0)
    product       = Column(String, default="")
    desired_value = Column(String, default="")
    urgency       = Column(String, default="")
    ai_summary    = Column(Text, default="")
    conversa      = Column(Text, default="")
    resumo        = Column(Text, default="")
    conversa_estado  = Column(Text, default="[]")
    wpp_etapa        = Column(String, default="")
    wpp_pendente     = Column(String, default="")
    wpp_dia_ref      = Column(String, default="")
    wpp_quer_meet    = Column(String, default="1")

    agendado_hora = Column(String, default="")
    scheduled_at  = Column(String, default="")
    especialista  = Column(String, default="")
    next_call_at  = Column(DateTime, nullable=True)

    call_attempts = Column(Integer, default=0)
    last_call_at  = Column(DateTime, nullable=True)
    call_sid      = Column(String, default="")
    wpp_phone     = Column(String, default="")    # WhatsApp diferente do telefone da ligação
    ia_pausada    = Column(Boolean, default=False) # True = IA não responde, humano atende
    especialista_id = Column(String, default="")   # ID do especialista atendendo
    parceira_indicada = Column(String, default="") # Tópico de parceira detectado na ligação
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sessions      = relationship("CallSession", back_populates="lead", cascade="all, delete-orphan")
    meetings      = relationship("Meeting", back_populates="lead", cascade="all, delete-orphan")
    wpp_mensagens = relationship("WppMensagem", back_populates="lead", cascade="all, delete-orphan", order_by="WppMensagem.created_at")


class CallSession(Base):
    __tablename__ = "call_sessions"

    id           = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id      = Column(String, ForeignKey("leads.id"))
    twilio_sid   = Column(String, default="")
    status       = Column(String, default="")
    duration_sec = Column(Integer, default=0)
    transcript   = Column(Text, default="")
    resumo       = Column(Text, default="")
    resultado    = Column(String, default="")
    started_at   = Column(DateTime, default=datetime.utcnow)

    lead         = relationship("Lead", back_populates="sessions")


class Meeting(Base):
    __tablename__ = "meetings"

    id                 = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id            = Column(String, ForeignKey("leads.id"))

    room_name          = Column(String, default="")
    room_url           = Column(String, default="")
    link_cliente       = Column(String, default="")
    link_especialista  = Column(String, default="")

    scheduled_at       = Column(String, default="")
    tipo               = Column(String, default="meet")

    status             = Column(String, default="agendado")

    recording_url      = Column(String, default="")
    recording_id       = Column(String, default="")
    transcricao_reuniao = Column(Text, default="")
    resumo_reuniao     = Column(Text, default="")
    transcript         = Column(Text, default="")
    token_host         = Column(String, default="")
    token_guest        = Column(String, default="")

    especialista       = Column(String, default="")
    lembrete_enviado   = Column(Boolean, default=False)

    created_at         = Column(DateTime, default=datetime.utcnow)
    updated_at         = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lead               = relationship("Lead", back_populates="meetings")


class WppMensagem(Base):
    __tablename__ = "wpp_mensagens"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id    = Column(String, ForeignKey("leads.id"), nullable=False)
    role       = Column(String, nullable=False)
    content    = Column(Text,   nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead       = relationship("Lead", back_populates="wpp_mensagens")


# ── CALLBACKS (ligar de volta) ────────────────────────────────────────────────

class Callback(Base):
    __tablename__ = "callbacks"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id       = Column(String, ForeignKey("leads.id"), nullable=False)
    scheduled_at  = Column(DateTime, nullable=False)
    status        = Column(String, default="pendente")   # pendente, executado, falhou, cancelado
    motivo        = Column(String, default="")            # "cliente pediu pra ligar às 15h"
    tentativas    = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)

    lead          = relationship("Lead")


# ── ESPECIALISTAS ─────────────────────────────────────────────────────────────

class Especialista(Base):
    __tablename__ = "especialistas"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    nome       = Column(String, nullable=False)
    area       = Column(String, default="")          # credito_pj, credito_pf, reestruturacao, etc
    titulo     = Column(String, default="")          # "Especialista em Crédito", "Suporte de TI"
    whatsapp   = Column(String, default="")          # número pessoal pra notificação
    email      = Column(String, default="")
    senha_hash = Column(String, default="")
    ativo      = Column(Boolean, default=True)
    online     = Column(Boolean, default=False)
    atendimentos_ativos = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)


class Transferencia(Base):
    __tablename__ = "transferencias"

    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id         = Column(String, ForeignKey("leads.id"), nullable=False)
    especialista_id = Column(String, ForeignKey("especialistas.id"), nullable=False)
    motivo          = Column(String, default="")
    status          = Column(String, default="ativa")  # ativa, encerrada
    contexto        = Column(Text, default="")          # resumo pra o especialista
    iniciada_em     = Column(DateTime, default=datetime.utcnow)
    encerrada_em    = Column(DateTime, nullable=True)

    lead         = relationship("Lead")
    especialista = relationship("Especialista")


# ── USUÁRIOS (autenticação) ───────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email         = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    name          = Column(String, default="")
    role          = Column(String, default="funcionario")  # "master" ou "funcionario"
    active        = Column(Boolean, default=True)

    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrar_colunas()
    _normalizar_telefones_existentes()
    _limpar_leads_ligando()
    _seed_master_user()


def _limpar_leads_ligando():
    """
    ✅ SAFETY NET: Leads que ficaram em "ligando" por mais de 30 minutos
    provavelmente tiveram falha no webhook pós-chamada.
    Volta pro stage anterior (nao_atendeu) pra não ficarem presos.
    """
    db = SessionLocal()
    try:
        from datetime import timedelta
        limite = datetime.utcnow() - timedelta(minutes=30)
        presos = db.query(Lead).filter(
            Lead.stage == "ligando",
            Lead.last_call_at < limite
        ).all()
        for lead in presos:
            print(f"⚠️ Lead preso em 'ligando': {lead.name} ({lead.phone}) — voltando para 'nao_atendeu'")
            lead.stage = "nao_atendeu"
        if presos:
            db.commit()
            print(f"✅ {len(presos)} leads destravados de 'ligando'")
    except Exception as e:
        print(f"⚠️ Erro ao limpar leads ligando: {e}")
    finally:
        db.close()


def _seed_master_user():
    """Cria o primeiro usuário master se não existir nenhum."""
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.role == "master").first()
        if existing:
            return  # Já tem master

        email = os.getenv("MASTER_EMAIL", "admin@flcbank.com.br")
        password = os.getenv("MASTER_PASSWORD", "")
        name = os.getenv("MASTER_NAME", "Administrador")

        print(f"🔍 MASTER_EMAIL={email}")
        print(f"🔍 MASTER_PASSWORD length={len(password)} bytes={len(password.encode('utf-8'))}")

        if not password:
            print("⚠️ MASTER_PASSWORD não definida — usuário master NÃO criado")
            print("   Defina MASTER_EMAIL e MASTER_PASSWORD nas variáveis de ambiente")
            return

        # Bcrypt tem limite de 72 bytes — trunca se necessário
        password = password[:72]
        print(f"🔑 Criando master: {email} (senha: {len(password)} chars)")

        from passlib.context import CryptContext
        pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

        master = User(
            email=email,
            password_hash=pwd_ctx.hash(password),
            name=name,
            role="master",
            active=True,
        )
        db.add(master)
        db.commit()
        print(f"✅ Usuário master criado: {email}")
    except Exception as e:
        print(f"⚠️ Erro ao criar master: {e}")
    finally:
        db.close()


def _migrar_colunas():
    migracoes = [
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS token_host VARCHAR DEFAULT ''",
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS token_guest VARCHAR DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS meetings_count INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS company VARCHAR DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS ia_pausada BOOLEAN DEFAULT FALSE",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS especialista_id VARCHAR DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS parceira_indicada VARCHAR DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS especialistas (
            id VARCHAR PRIMARY KEY,
            nome VARCHAR NOT NULL,
            area VARCHAR DEFAULT '',
            titulo VARCHAR DEFAULT '',
            whatsapp VARCHAR DEFAULT '',
            email VARCHAR DEFAULT '',
            senha_hash VARCHAR DEFAULT '',
            ativo BOOLEAN DEFAULT TRUE,
            online BOOLEAN DEFAULT FALSE,
            atendimentos_ativos INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS transferencias (
            id VARCHAR PRIMARY KEY,
            lead_id VARCHAR REFERENCES leads(id),
            especialista_id VARCHAR REFERENCES especialistas(id),
            motivo VARCHAR DEFAULT '',
            status VARCHAR DEFAULT 'ativa',
            contexto TEXT DEFAULT '',
            iniciada_em TIMESTAMP DEFAULT NOW(),
            encerrada_em TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS callbacks (
            id VARCHAR PRIMARY KEY,
            lead_id VARCHAR REFERENCES leads(id),
            scheduled_at TIMESTAMP NOT NULL,
            status VARCHAR DEFAULT 'pendente',
            motivo VARCHAR DEFAULT '',
            tentativas INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS recording_id VARCHAR DEFAULT ''",
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS transcricao_reuniao TEXT DEFAULT ''",
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS resumo_reuniao TEXT DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS conversa_estado TEXT DEFAULT '[]'",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS wpp_etapa VARCHAR DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS wpp_pendente VARCHAR DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS wpp_dia_ref VARCHAR DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS wpp_quer_meet VARCHAR DEFAULT '1'",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS scheduled_at VARCHAR DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS wpp_phone VARCHAR DEFAULT ''",
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS lembrete_enviado BOOLEAN DEFAULT FALSE",
        "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS transcript TEXT DEFAULT ''",
        "CREATE TABLE IF NOT EXISTS wpp_mensagens (id VARCHAR PRIMARY KEY, lead_id VARCHAR NOT NULL REFERENCES leads(id), role VARCHAR NOT NULL, content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS users (id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL, password_hash VARCHAR NOT NULL, name VARCHAR DEFAULT '', role VARCHAR DEFAULT 'funcionario', active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW())",
    ]
    with engine.connect() as conn:
        for sql in migracoes:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                print(f"⚠️ Migração ignorada: {e}")


def _normalizar_telefones_existentes():
    """
    ✅ MIGRAÇÃO: Normaliza todos os telefones existentes no banco.
    '(51) 99746-4857' → '5551997464857'
    
    Roda no startup. Se o telefone já é só dígitos, não muda nada.
    """
    db = SessionLocal()
    try:
        leads = db.query(Lead).all()
        alterados = 0
        for lead in leads:
            if not lead.phone:
                continue
            normalizado = normalizar_telefone(lead.phone)
            if normalizado != lead.phone:
                print(f"📱 Normalizando: '{lead.phone}' → '{normalizado}' ({lead.name or 'sem nome'})")
                lead.phone = normalizado
                alterados += 1
        if alterados > 0:
            db.commit()
            print(f"✅ {alterados} telefones normalizados")
    except Exception as e:
        print(f"⚠️ Erro ao normalizar telefones: {e}")
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()