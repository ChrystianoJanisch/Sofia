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
from api.callback_scheduler import executar_callbacks, executar_followups
from api.routes_leads import router as leads_router
from api.routes_calls import router as calls_router
from api.routes_crm import router as crm_router
from api.routes_whatsapp import router as whatsapp_router
from api.routes_meetings import router as meetings_router
from api.routes_auth import router as auth_router          # ← AUTH
from api.routes_especialistas import router as esp_router   # ← ESPECIALISTAS
from api.routes_daily_webhook import router as daily_webhook_router
from api.routes_analytics import router as analytics_router

STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title=os.getenv("APP_NAME", "Julia"), version="6.1.0")

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
app.include_router(esp_router,      prefix="/api/especialistas", tags=["Especialistas"])
app.include_router(daily_webhook_router, prefix="/api/daily", tags=["Daily.co"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["Analytics"])


async def _loop_limpar_ligando():
    """Loop que roda a cada 2 min para destravar leads presos em 'ligando'."""
    import asyncio
    from db.database import _limpar_leads_ligando
    while True:
        await asyncio.sleep(60)
        try:
            _limpar_leads_ligando()
        except Exception as e:
            print(f"⚠️ Erro no loop limpar_ligando: {e}")


@app.on_event("startup")
async def startup():
    init_db()
    iniciar_scheduler()
    import asyncio
    asyncio.create_task(executar_callbacks())
    asyncio.create_task(executar_followups())
    asyncio.create_task(_loop_limpar_ligando())
    print("🚀 Sofia v6.2 — Auth + Agenda + WhatsApp + Callbacks + Analytics — Pronta!")


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


@app.get("/analytics")
def analytics_page():
    return FileResponse(os.path.join(STATIC_DIR, "analytics.html"))


@app.get("/painel-especialista")
def painel_especialista():
    return FileResponse(os.path.join(STATIC_DIR, "especialista.html"))