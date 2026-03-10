from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import sys, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
load_dotenv()

from db.database import init_db
from scheduler import iniciar_scheduler
from api.routes_leads import router as leads_router
from api.routes_calls import router as calls_router
from api.routes_crm import router as crm_router
from api.routes_whatsapp import router as whatsapp_router
from api.routes_meetings import router as meetings_router
from api.routes_auth import router as auth_router          # ← AUTH

STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title=os.getenv("APP_NAME", "Julia.ia"), version="6.1.0")

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Routers
app.include_router(leads_router,    prefix="/api/leads",    tags=["Leads"])
app.include_router(calls_router,    prefix="/api/calls",    tags=["Ligações"])
app.include_router(crm_router,      prefix="/api/crm",      tags=["CRM"])
app.include_router(whatsapp_router, prefix="/api/whatsapp", tags=["WhatsApp"])
app.include_router(meetings_router, prefix="/reuniao",      tags=["Reuniões"])
app.include_router(auth_router,     prefix="/api/auth",     tags=["Auth"])  # ← AUTH


@app.on_event("startup")
async def startup():
    init_db()
    iniciar_scheduler()
    print("🚀 Julia.ia v6.1 — Auth + Agenda + WhatsApp — Pronta!")


@app.get("/")
def home():
    return {
        "status": "online",
        "versao": "6.1.0",
        "dashboard": "/dashboard",
        "login": "/login",
    }


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/dashboard")
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))