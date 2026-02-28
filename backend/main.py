from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import sys, os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from db.database import init_db
from api.routes_leads import router as leads_router
from api.routes_calls import router as calls_router

app = FastAPI(title=os.getenv("APP_NAME", "Sofia AI"), version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=[""], allow_methods=[""], allow_headers=["*"])

app.include_router(leads_router, prefix="/api/leads", tags=["Leads"])
app.include_router(calls_router, prefix="/api/calls", tags=["Ligações"])

@app.on_event("startup")
def startup():
    init_db()
    print("✅ Sofia AI v3 — Pronta para ligar!")

@app.get("/")
def home():
    return {"status": "online", "versao": "3.0.0"}