from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Float, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import uuid, os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sofia.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Lead(Base):
    __tablename__ = "leads"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name          = Column(String)
    phone         = Column(String, nullable=False)
    email         = Column(String)
    temperature   = Column(String, default="pending")
    score         = Column(Float, default=0)
    product       = Column(String)
    desired_value = Column(String)
    urgency       = Column(String)
    ai_summary    = Column(Text)
    stage         = Column(String, default="new")
    call_attempts = Column(Integer, default=0)
    last_call_at  = Column(DateTime)
    created_at    = Column(DateTime, default=datetime.utcnow)

class CallSession(Base):
    __tablename__ = "call_sessions"

    id           = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id      = Column(String, ForeignKey("leads.id"))
    twilio_sid   = Column(String)
    status       = Column(String)
    duration_sec = Column(Integer)
    transcript   = Column(Text)
    started_at   = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()