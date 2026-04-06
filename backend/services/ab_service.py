"""
A/B Testing Service — Testa variações de abordagem automaticamente.
Cada teste compara duas variantes e mede qual converte melhor.
"""
import os, json, random, math
from datetime import datetime
from sqlalchemy.orm import Session
from db.database import ABTest, ABResult, Lead, CallSession

IA_NAME = os.getenv("IA_NAME", "Sofia")


def criar_teste(name: str, description: str, variant_a: dict, variant_b: dict, db: Session) -> ABTest:
    """Cria um novo teste A/B."""
    teste = ABTest(
        name=name,
        description=description,
        variant_a_name=variant_a.get("name", "A"),
        variant_a_config=json.dumps(variant_a, ensure_ascii=False),
        variant_b_name=variant_b.get("name", "B"),
        variant_b_config=json.dumps(variant_b, ensure_ascii=False),
        metric=variant_a.get("metric", "conversion_rate"),
        status="active",
    )
    db.add(teste)
    db.commit()
    db.refresh(teste)
    print(f"🧪 Teste A/B criado: '{name}' ({teste.id})")
    return teste


def selecionar_variante(test_id: str, db: Session) -> tuple[str, dict]:
    """
    Seleciona variante A ou B para o próximo lead.
    Usa epsilon-greedy: 80% explora a melhor, 20% explora a outra.
    """
    teste = db.get(ABTest, test_id)
    if not teste or teste.status != "active":
        return "A", {}

    # Calcula taxa de conversão de cada variante
    rate_a = (teste.conversions_a / teste.total_a) if teste.total_a > 0 else 0.5
    rate_b = (teste.conversions_b / teste.total_b) if teste.total_b > 0 else 0.5

    # Epsilon-greedy: 80% melhor, 20% exploratório
    epsilon = 0.2
    if random.random() < epsilon:
        variante = random.choice(["A", "B"])
    else:
        variante = "A" if rate_a >= rate_b else "B"

    config = json.loads(teste.variant_a_config if variante == "A" else teste.variant_b_config)
    return variante, config


def registrar_resultado(test_id: str, lead_id: str, variant: str, outcome: str,
                        temperature: str = "", duration_sec: int = 0,
                        call_id: str = None, db: Session = None) -> ABResult:
    """Registra resultado de uma ligação no teste A/B."""
    resultado = ABResult(
        test_id=test_id,
        lead_id=lead_id,
        call_id=call_id,
        variant=variant,
        outcome=outcome,
        temperature=temperature,
        duration_sec=duration_sec,
    )
    db.add(resultado)

    # Atualiza contadores do teste
    teste = db.get(ABTest, test_id)
    if teste:
        if variant == "A":
            teste.total_a += 1
            if outcome in ("hot", "warm", "converteu", "interessado"):
                teste.conversions_a += 1
        else:
            teste.total_b += 1
            if outcome in ("hot", "warm", "converteu", "interessado"):
                teste.conversions_b += 1

        # Calcula confiança estatística (z-test simplificado)
        teste.confidence = _calcular_confianca(
            teste.total_a, teste.conversions_a,
            teste.total_b, teste.conversions_b
        )

        # Auto-encerra se confiança > 95% e mínimo 30 amostras por variante
        if teste.confidence >= 95 and teste.total_a >= 30 and teste.total_b >= 30:
            rate_a = teste.conversions_a / teste.total_a
            rate_b = teste.conversions_b / teste.total_b
            teste.winner = "A" if rate_a > rate_b else "B"
            teste.status = "completed"
            teste.completed_at = datetime.utcnow()
            print(f"🏆 Teste '{teste.name}' encerrado! Vencedor: variante {teste.winner} (confiança: {teste.confidence:.1f}%)")

    db.commit()
    return resultado


def _calcular_confianca(n_a: int, conv_a: int, n_b: int, conv_b: int) -> float:
    """Calcula confiança estatística entre duas variantes (z-test)."""
    if n_a < 5 or n_b < 5:
        return 0.0

    p_a = conv_a / n_a
    p_b = conv_b / n_b
    p_pool = (conv_a + conv_b) / (n_a + n_b)

    if p_pool == 0 or p_pool == 1:
        return 0.0

    se = math.sqrt(p_pool * (1 - p_pool) * (1/n_a + 1/n_b))
    if se == 0:
        return 0.0

    z = abs(p_a - p_b) / se

    # Aproximação: z=1.96 -> 95%, z=2.58 -> 99%
    if z >= 2.58:
        return 99.0
    elif z >= 1.96:
        return 95.0 + (z - 1.96) / (2.58 - 1.96) * 4
    elif z >= 1.645:
        return 90.0 + (z - 1.645) / (1.96 - 1.645) * 5
    else:
        return min(z / 1.645 * 90, 89.9)


def listar_testes(db: Session, status: str = None) -> list[dict]:
    """Lista todos os testes A/B."""
    query = db.query(ABTest).order_by(ABTest.created_at.desc())
    if status:
        query = query.filter(ABTest.status == status)
    testes = query.all()

    return [{
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "status": t.status,
        "variant_a": {"name": t.variant_a_name, "total": t.total_a, "conversions": t.conversions_a,
                       "rate": round(t.conversions_a / t.total_a * 100, 1) if t.total_a > 0 else 0},
        "variant_b": {"name": t.variant_b_name, "total": t.total_b, "conversions": t.conversions_b,
                       "rate": round(t.conversions_b / t.total_b * 100, 1) if t.total_b > 0 else 0},
        "winner": t.winner,
        "confidence": round(t.confidence, 1),
        "created_at": str(t.created_at),
        "completed_at": str(t.completed_at) if t.completed_at else None,
    } for t in testes]


def obter_teste_ativo(db: Session) -> ABTest | None:
    """Retorna o teste A/B ativo (só 1 por vez)."""
    return db.query(ABTest).filter(ABTest.status == "active").first()
