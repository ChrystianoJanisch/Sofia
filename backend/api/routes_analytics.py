"""
Routes Analytics — Métricas de performance e gestão de testes A/B.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from db.database import get_db, Lead, CallSession, Meeting, CallInsight, ABTest, ABResult
from datetime import datetime, timedelta
import os

router = APIRouter()

IA_NAME = os.getenv("IA_NAME", "Sofia")
EMPRESA_NOME = os.getenv("EMPRESA_NOME", "FLC Bank")


# ═══════════════════════════════════════════════════════════════
# MÉTRICAS GERAIS
# ═══════════════════════════════════════════════════════════════

@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    """Dashboard overview — métricas principais."""
    agora = datetime.utcnow()
    hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    semana = agora - timedelta(days=7)
    mes = agora - timedelta(days=30)

    # Totais
    total_leads = db.query(Lead).count()
    leads_hoje = db.query(Lead).filter(Lead.created_at >= hoje).count()

    # Calls
    total_calls = db.query(CallSession).count()
    calls_hoje = db.query(CallSession).filter(CallSession.started_at >= hoje).count()
    calls_semana = db.query(CallSession).filter(CallSession.started_at >= semana).count()

    # Duração média
    avg_duration = db.query(func.avg(CallSession.duration_sec)).filter(
        CallSession.duration_sec > 15
    ).scalar() or 0

    # Conversão por stage
    interessados = db.query(Lead).filter(Lead.stage == "interessado").count()
    sem_interesse = db.query(Lead).filter(Lead.stage == "sem_interesse").count()
    callbacks = db.query(Lead).filter(Lead.stage == "callback_agendado").count()
    nao_atendeu = db.query(Lead).filter(Lead.stage == "nao_atendeu").count()

    # Taxa de conversão (interessados / total que atendeu)
    total_contatados = interessados + sem_interesse + callbacks
    taxa_conversao = round((interessados / total_contatados * 100), 1) if total_contatados > 0 else 0

    # Reuniões
    reunioes_agendadas = db.query(Meeting).filter(Meeting.status == "agendado").count()
    reunioes_realizadas = db.query(Meeting).filter(Meeting.status == "realizada").count()

    # Temperatura
    hot = db.query(Lead).filter(Lead.temperature == "hot").count()
    warm = db.query(Lead).filter(Lead.temperature == "warm").count()
    cold = db.query(Lead).filter(Lead.temperature == "cold").count()

    return {
        "ia_name": IA_NAME,
        "empresa": EMPRESA_NOME,
        "timestamp": agora.isoformat(),
        "leads": {
            "total": total_leads,
            "hoje": leads_hoje,
        },
        "calls": {
            "total": total_calls,
            "hoje": calls_hoje,
            "semana": calls_semana,
            "duracao_media_seg": round(avg_duration),
        },
        "conversao": {
            "taxa": taxa_conversao,
            "interessados": interessados,
            "sem_interesse": sem_interesse,
            "callbacks": callbacks,
            "nao_atendeu": nao_atendeu,
        },
        "temperatura": {
            "hot": hot,
            "warm": warm,
            "cold": cold,
        },
        "reunioes": {
            "agendadas": reunioes_agendadas,
            "realizadas": reunioes_realizadas,
        },
    }


@router.get("/calls/by-day")
def calls_por_dia(dias: int = 30, db: Session = Depends(get_db)):
    """Ligações por dia nos últimos N dias."""
    desde = datetime.utcnow() - timedelta(days=dias)
    calls = db.query(CallSession).filter(CallSession.started_at >= desde).all()

    por_dia = {}
    for c in calls:
        dia = c.started_at.strftime("%Y-%m-%d") if c.started_at else "desconhecido"
        if dia not in por_dia:
            por_dia[dia] = {"total": 0, "converteu": 0, "duracao_total": 0}
        por_dia[dia]["total"] += 1
        if c.resultado in ("hot", "warm"):
            por_dia[dia]["converteu"] += 1
        por_dia[dia]["duracao_total"] += c.duration_sec or 0

    resultado = []
    for dia, dados in sorted(por_dia.items()):
        resultado.append({
            "data": dia,
            "total": dados["total"],
            "converteu": dados["converteu"],
            "taxa": round(dados["converteu"] / dados["total"] * 100, 1) if dados["total"] > 0 else 0,
            "duracao_media": round(dados["duracao_total"] / dados["total"]) if dados["total"] > 0 else 0,
        })

    return {"dias": resultado}


@router.get("/calls/by-hour")
def calls_por_hora(db: Session = Depends(get_db)):
    """Performance por hora do dia — melhor horário pra ligar."""
    calls = db.query(CallSession).filter(CallSession.duration_sec > 15).all()

    por_hora = {}
    for c in calls:
        if not c.started_at:
            continue
        hora = c.started_at.hour
        if hora not in por_hora:
            por_hora[hora] = {"total": 0, "converteu": 0}
        por_hora[hora]["total"] += 1
        if c.resultado in ("hot", "warm"):
            por_hora[hora]["converteu"] += 1

    resultado = []
    for hora in range(8, 21):  # 8h às 20h
        dados = por_hora.get(hora, {"total": 0, "converteu": 0})
        resultado.append({
            "hora": f"{hora:02d}:00",
            "total": dados["total"],
            "converteu": dados["converteu"],
            "taxa": round(dados["converteu"] / dados["total"] * 100, 1) if dados["total"] > 0 else 0,
        })

    return {"horas": resultado}


# ═══════════════════════════════════════════════════════════════
# INSIGHTS / CRITIQUE
# ═══════════════════════════════════════════════════════════════

@router.get("/insights")
def listar_insights(limite: int = 20, db: Session = Depends(get_db)):
    """Lista insights de ligações recentes."""
    insights = db.query(CallInsight).order_by(CallInsight.created_at.desc()).limit(limite).all()

    return [{
        "id": i.id,
        "lead_id": i.lead_id,
        "approach": i.approach_used,
        "outcome": i.outcome,
        "temperature": i.temperature,
        "duration_sec": i.duration_sec,
        "what_worked": i.what_worked,
        "what_failed": i.what_failed,
        "suggestion": i.suggestion,
        "engagement": i.client_engagement,
        "sentiment": i.sentiment_score,
        "created_at": str(i.created_at),
    } for i in insights]


@router.get("/insights/summary")
def resumo_insights(db: Session = Depends(get_db)):
    """Resumo agregado dos aprendizados."""
    from services.critique_service import gerar_resumo_aprendizados
    resumo = gerar_resumo_aprendizados(db)

    # Métricas de engagement
    desde = datetime.utcnow() - timedelta(days=30)
    insights = db.query(CallInsight).filter(CallInsight.created_at >= desde).all()

    avg_engagement = sum(i.client_engagement for i in insights) / len(insights) if insights else 0
    avg_sentiment = sum(i.sentiment_score for i in insights) / len(insights) if insights else 0

    # Top abordagens
    from collections import Counter
    abordagens = Counter(i.approach_used for i in insights if i.approach_used)

    return {
        "resumo_texto": resumo,
        "total_insights": len(insights),
        "engagement_medio": round(avg_engagement, 1),
        "sentimento_medio": round(avg_sentiment, 2),
        "top_abordagens": [{"tipo": k, "count": v} for k, v in abordagens.most_common(5)],
    }


# ═══════════════════════════════════════════════════════════════
# A/B TESTING
# ═══════════════════════════════════════════════════════════════

@router.get("/ab-tests")
def listar_ab_tests(db: Session = Depends(get_db)):
    """Lista todos os testes A/B."""
    from services.ab_service import listar_testes
    return {"testes": listar_testes(db)}


@router.post("/ab-tests")
def criar_ab_test(body: dict, db: Session = Depends(get_db)):
    """Cria um novo teste A/B."""
    from services.ab_service import criar_teste
    teste = criar_teste(
        name=body.get("name", "Teste sem nome"),
        description=body.get("description", ""),
        variant_a=body.get("variant_a", {"name": "A"}),
        variant_b=body.get("variant_b", {"name": "B"}),
        db=db,
    )
    return {"sucesso": True, "test_id": teste.id}


@router.post("/ab-tests/{test_id}/pause")
def pausar_ab_test(test_id: str, db: Session = Depends(get_db)):
    """Pausa um teste A/B."""
    teste = db.get(ABTest, test_id)
    if not teste:
        return {"erro": "Teste não encontrado"}
    teste.status = "paused" if teste.status == "active" else "active"
    db.commit()
    return {"sucesso": True, "status": teste.status}


@router.get("/ab-tests/{test_id}/results")
def resultados_ab_test(test_id: str, db: Session = Depends(get_db)):
    """Resultados detalhados de um teste A/B."""
    teste = db.get(ABTest, test_id)
    if not teste:
        return {"erro": "Teste não encontrado"}

    resultados = db.query(ABResult).filter(ABResult.test_id == test_id).order_by(ABResult.created_at.desc()).all()

    return {
        "teste": {
            "id": teste.id,
            "name": teste.name,
            "status": teste.status,
            "winner": teste.winner,
            "confidence": round(teste.confidence, 1),
        },
        "variante_a": {
            "name": teste.variant_a_name,
            "total": teste.total_a,
            "conversions": teste.conversions_a,
            "rate": round(teste.conversions_a / teste.total_a * 100, 1) if teste.total_a > 0 else 0,
        },
        "variante_b": {
            "name": teste.variant_b_name,
            "total": teste.total_b,
            "conversions": teste.conversions_b,
            "rate": round(teste.conversions_b / teste.total_b * 100, 1) if teste.total_b > 0 else 0,
        },
        "resultados": [{
            "variant": r.variant,
            "outcome": r.outcome,
            "temperature": r.temperature,
            "duration_sec": r.duration_sec,
            "created_at": str(r.created_at),
        } for r in resultados[:50]],
    }
