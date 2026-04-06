"""
Critique Service — Auto-aprendizado da IA
Analisa cada ligação e gera insights para melhorar futuras abordagens.
"""
import os, json
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from db.database import CallInsight, CallSession, Lead, SessionLocal

IA_NAME = os.getenv("IA_NAME", "Sofia")
EMPRESA_NOME = os.getenv("EMPRESA_NOME", "FLC Bank")


def analisar_ligacao(call_id: str, db: Session) -> dict | None:
    """
    Analisa uma ligação e gera insights.
    Chamado automaticamente após cada pos-chamada.
    """
    call = db.query(CallSession).filter(CallSession.id == call_id).first()
    if not call or not call.transcript:
        return None

    lead = db.get(Lead, call.lead_id)
    if not lead:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # Busca insights anteriores pra contextualizar
        insights_anteriores = db.query(CallInsight).order_by(
            CallInsight.created_at.desc()
        ).limit(5).all()

        contexto_anterior = ""
        if insights_anteriores:
            aprendizados = [i.suggestion for i in insights_anteriores if i.suggestion]
            if aprendizados:
                contexto_anterior = f"\nAPRENDIZADOS ANTERIORES:\n" + "\n".join(f"- {a}" for a in aprendizados[:3])

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": f"""Analise esta ligação de prospecção da {EMPRESA_NOME} (banco).

TRANSCRIÇÃO:
{call.transcript[-3000:]}

RESULTADO: {call.resultado} | DURAÇÃO: {call.duration_sec}s | TEMPERATURA: {lead.temperature}
{contexto_anterior}

Responda APENAS em JSON:
{{
    "approach_used": "tipo de abordagem (consultiva/direta/empática/técnica)",
    "opening_style": "como abriu a conversa (resumo curto)",
    "objection_handled": "objeções que apareceram e como foram tratadas (ou vazio)",
    "what_worked": "o que funcionou bem nessa ligação (1-2 frases)",
    "what_failed": "o que não funcionou ou poderia melhorar (1-2 frases)",
    "suggestion": "sugestão concreta para melhorar a próxima ligação similar (1 frase)",
    "client_engagement": 0-10,
    "sentiment_score": -1.0 a 1.0
}}"""
            }],
            max_tokens=500,
            temperature=0
        )

        texto = resp.choices[0].message.content.strip()
        texto = texto.replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto)

        insight = CallInsight(
            call_id=call_id,
            lead_id=call.lead_id,
            approach_used=resultado.get("approach_used", ""),
            opening_style=resultado.get("opening_style", ""),
            objection_handled=resultado.get("objection_handled", ""),
            outcome=call.resultado or "",
            temperature=lead.temperature or "",
            duration_sec=call.duration_sec or 0,
            what_worked=resultado.get("what_worked", ""),
            what_failed=resultado.get("what_failed", ""),
            suggestion=resultado.get("suggestion", ""),
            client_engagement=float(resultado.get("client_engagement", 0)),
            sentiment_score=float(resultado.get("sentiment_score", 0)),
        )
        db.add(insight)
        db.commit()

        print(f"🧠 Insight gerado para {lead.name}: {resultado.get('suggestion', '')[:80]}")
        return resultado

    except Exception as e:
        print(f"⚠️ Erro ao gerar insight: {e}")
        return None


def obter_aprendizados_recentes(db: Session, limite: int = 10) -> list[dict]:
    """Retorna os insights mais recentes para alimentar prompts."""
    insights = db.query(CallInsight).order_by(
        CallInsight.created_at.desc()
    ).limit(limite).all()

    return [{
        "approach": i.approach_used,
        "outcome": i.outcome,
        "what_worked": i.what_worked,
        "suggestion": i.suggestion,
        "engagement": i.client_engagement,
    } for i in insights]


def gerar_resumo_aprendizados(db: Session) -> str:
    """
    Gera um resumo dos aprendizados pra injetar nos prompts da IA.
    Chamado periodicamente ou antes de ligações.
    """
    # Últimos 30 dias
    desde = datetime.utcnow() - timedelta(days=30)
    insights = db.query(CallInsight).filter(
        CallInsight.created_at >= desde
    ).all()

    if not insights:
        return ""

    # Calcula métricas
    total = len(insights)
    conversoes = len([i for i in insights if i.outcome in ("hot", "warm", "converteu")])
    taxa = (conversoes / total * 100) if total > 0 else 0

    # Top abordagens
    from collections import Counter
    abordagens_boas = Counter()
    for i in insights:
        if i.outcome in ("hot", "warm", "converteu") and i.approach_used:
            abordagens_boas[i.approach_used] += 1

    top_abordagem = abordagens_boas.most_common(1)

    # Sugestões mais recentes
    sugestoes = [i.suggestion for i in insights[-5:] if i.suggestion]

    resumo = f"APRENDIZADOS DAS ÚLTIMAS {total} LIGAÇÕES (taxa conversão: {taxa:.0f}%):\n"
    if top_abordagem:
        resumo += f"- Melhor abordagem: {top_abordagem[0][0]} ({top_abordagem[0][1]} conversões)\n"
    if sugestoes:
        resumo += "- Sugestões recentes:\n"
        for s in sugestoes[-3:]:
            resumo += f"  • {s}\n"

    return resumo
