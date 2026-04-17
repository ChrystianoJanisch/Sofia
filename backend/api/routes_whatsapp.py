import os, json
from fastapi import APIRouter, Request, Depends, Body
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from db.database import get_db, Lead, Meeting, WppMensagem, normalizar_telefone
from integrations.whatsapp import _enviar, enviar_dados_institucionais  # type: ignore
from integrations.daily import criar_sala  # type: ignore
from datetime import datetime

router = APIRouter()

# Variáveis de configuração da IA
IA_NAME = os.getenv("IA_NAME", "Julia")
EMPRESA_NOME = os.getenv("EMPRESA_NOME", "FLC Bank")


def _normalizar_numero(numero: str) -> str:
    """
    Normaliza número brasileiro para formato padrão: 55 + DDD + 9 + 8 dígitos.
    Resolve o problema de duplicatas quando o CRM tem o número sem o 9.
    """
    digits = "".join(c for c in numero if c.isdigit())
    if not digits.startswith("55"):
        digits = "55" + digits
    if len(digits) == 12:
        digits = digits[:4] + "9" + digits[4:]
    return digits


def _buscar_lead_por_numero(numero: str, db: Session):
    """
    Busca lead no CRM tolerando variações com/sem o dígito 9.
    
    ✅ CORREÇÃO: Se .contains() não encontrar (telefone com formatação),
    carrega todos os leads e compara apenas os dígitos.
    """
    digits = "".join(c for c in numero if c.isdigit())

    def _query(sufixo: str):
        from sqlalchemy import or_
        candidatos = db.query(Lead).filter(
            or_(Lead.phone.contains(sufixo), Lead.wpp_phone.contains(sufixo))
        ).all()
        if not candidatos:
            return None
        com_nome = [l for l in candidatos if l.name and l.name.strip()]
        if com_nome:
            nao_novos = [l for l in com_nome if l.stage not in ("novo", "")]
            return nao_novos[0] if nao_novos else com_nome[0]
        nao_novos = [l for l in candidatos if l.stage not in ("novo", "")]
        return nao_novos[0] if nao_novos else candidatos[0]

    def _priorizar(candidatos):
        """Prioriza leads com nome e stage avançado."""
        if not candidatos:
            return None
        com_nome = [l for l in candidatos if l.name and l.name.strip()]
        if com_nome:
            nao_novos = [l for l in com_nome if l.stage not in ("novo", "")]
            return nao_novos[0] if nao_novos else com_nome[0]
        nao_novos = [l for l in candidatos if l.stage not in ("novo", "")]
        return nao_novos[0] if nao_novos else candidatos[0]

    # Tentativa 1: busca SQL com últimos 8 dígitos
    lead = _query(digits[-8:])
    if lead:
        return lead

    # Tentativa 2: sem o 9 após DDD
    if len(digits) >= 12:
        sem_nove = digits[:4] + digits[5:]
        lead = _query(sem_nove[-8:])
        if lead:
            return lead

    # Tentativa 3: com o 9 adicionado
    if len(digits) == 12:
        com_nove = digits[:4] + "9" + digits[4:]
        lead = _query(com_nove[-8:])
        if lead:
            return lead

    # ✅ Tentativa 4 (FALLBACK): busca por dígitos puros
    # Se o telefone no banco tem formatação (ex: "(51) 99746-4857"),
    # .contains() não encontra. Aqui comparamos só os dígitos.
    sufixo = digits[-8:]
    todos = db.query(Lead).all()
    matches = []
    for l in todos:
        phone_digits = "".join(c for c in (l.phone or "") if c.isdigit())
        wpp_digits = "".join(c for c in (l.wpp_phone or "") if c.isdigit())
        if phone_digits.endswith(sufixo) or sufixo in phone_digits or \
           wpp_digits.endswith(sufixo) or sufixo in wpp_digits:
            matches.append(l)
    if matches:
        found = _priorizar(matches)
        if found:
            # ✅ Normaliza o telefone do lead encontrado para evitar esse problema de novo
            novo_phone = normalizar_telefone(found.phone)
            if novo_phone != found.phone:
                print(f"📱 Auto-normalizando telefone: '{found.phone}' → '{novo_phone}'")
                found.phone = novo_phone
                db.commit()
            return found

    return None


def _get_estado(lead, db: Session) -> dict:
    """
    Carrega estado da conversa.
    
    ✅ CORREÇÃO: Fonte da verdade é wpp_mensagens.
    Se estiver vazio, faz fallback para conversa_estado (compatibilidade
    com mensagens enviadas antes desta correção).
    """
    msgs = db.query(WppMensagem)\
             .filter(WppMensagem.lead_id == lead.id)\
             .order_by(WppMensagem.created_at)\
             .all()

    # Filtra mensagens de "system" — são notas internas (contexto da ligação)
    # A IA vai receber isso separadamente via _gerar_resposta_ia
    historico = [
        {"role": m.role, "content": m.content}
        for m in msgs
        if m.role in ("user", "assistant")
    ]

    # ✅ Fallback: se wpp_mensagens está vazio mas conversa_estado tem dados
    if not historico:
        try:
            estado_json = json.loads(lead.conversa_estado or "[]")
            if isinstance(estado_json, list) and estado_json:
                historico = [
                    {"role": m.get("role", "assistant"), "content": m.get("content", "")}
                    for m in estado_json
                    if m.get("content") and m.get("role") in ("user", "assistant")
                ]
                # Migra para wpp_mensagens para não precisar do fallback de novo
                for m in historico:
                    _salvar_msg(lead.id, m["role"], m["content"], db)
                print(f"📋 Migrado {len(historico)} msgs de conversa_estado → wpp_mensagens para lead {lead.id}")
        except (json.JSONDecodeError, TypeError):
            pass

    etapa = lead.wpp_etapa or "conversa"
    if etapa in ("conversa", "pos_ligacao") and not historico and lead.stage == "nao_atendeu":
        etapa = "pos_ligacao"

    # ✅ CORREÇÃO: Carregar dados de agendamento do conversa_estado (JSON)
    extras = {}
    try:
        raw = json.loads(lead.conversa_estado or "{}")
        if isinstance(raw, dict):
            extras = raw
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "historico":            historico,
        "etapa":                etapa,
        "data_hora_pendente":   lead.wpp_pendente or "",
        "dia_referencia":       lead.wpp_dia_ref or "",
        "quer_meet":            (lead.wpp_quer_meet or "1") == "1",
        "datas_disponiveis":    extras.get("datas_disponiveis", []),
        "slots_disponiveis":    extras.get("slots_disponiveis", []),
        "semana_offset":        extras.get("semana_offset", 0),
        "turno":                extras.get("turno", ""),
        "tipo_reuniao":         extras.get("tipo_reuniao", ""),
    }


def _salvar_msg(lead_id: str, role: str, content: str, db: Session, wamid: str = ""):
    """Persiste uma única mensagem na tabela wpp_mensagens."""
    msg = WppMensagem(lead_id=lead_id, role=role, content=content, wamid=wamid, status="sent" if role == "assistant" else "")
    db.add(msg)
    db.commit()


def _save_estado(lead, estado: dict, db: Session):
    """Persiste metadados de estado — etapa, horários, preferências."""
    lead.wpp_etapa    = estado.get("etapa", "")
    lead.wpp_pendente = estado.get("data_hora_pendente", "")
    lead.wpp_dia_ref  = estado.get("dia_referencia", "")
    lead.wpp_quer_meet = "1" if estado.get("quer_meet", True) else "0"

    # ✅ CORREÇÃO: Salvar dados de agendamento (datas, slots, offset) em conversa_estado
    extras = {
        "datas_disponiveis": estado.get("datas_disponiveis", []),
        "slots_disponiveis": estado.get("slots_disponiveis", []),
        "semana_offset":     estado.get("semana_offset", 0),
        "turno":             estado.get("turno", ""),
        "tipo_reuniao":      estado.get("tipo_reuniao", ""),
    }
    lead.conversa_estado = json.dumps(extras, ensure_ascii=False)
    db.commit()


def _responder(numero: str, conteudo: str, lead_id: str, estado: dict, db: Session):
    """
    Envia mensagem WhatsApp, salva no banco e atualiza histórico em memória.
    Único ponto de saída — garante que TUDO que a IA envia fica registrado.
    """
    wamid = _enviar(numero, conteudo) or ""
    _salvar_msg(lead_id, "assistant", conteudo, db, wamid=wamid)
    estado["historico"].append({"role": "assistant", "content": conteudo})


def _extrair_nome(texto: str) -> str:
    """
    Extrai o nome de uma mensagem do WhatsApp.
    Retorna string vazia se não conseguir identificar um nome válido.
    """
    import re
    
    t = texto.strip()
    
    # Remove emojis
    t = re.sub(r'[\U00010000-\U0010ffff]', '', t, flags=re.UNICODE).strip()
    
    # Remove pontuação no final
    t = re.sub(r'[.!?,;:]+$', '', t).strip()
    
    # ── DETECÇÃO DE FRASES QUE NÃO SÃO NOMES ────────────────────────
    t_lower = t.lower().strip()
    
    # Frases que começam com verbos/perguntas = NÃO é nome
    # NOTA: "me chamo", "me chamam" são expressões de apresentação, não bloqueiam
    nao_eh_nome_inicio = [
        "queria", "quero", "preciso", "gostaria", "tenho", "posso",
        "pode", "como", "quando", "onde", "qual", "quanto", "porque",
        "por que", "será", "sera", "tem", "vocês", "voces",
        "estou", "tô ", "to ", "vim", "vi ", "soube", "alguém",
        "ajuda", "ajudem", "informação", "informacao", "dúvida", "duvida",
        "sobre", "pra ", "para ", "não", "nao", "sim", "ok",
        "me ajuda", "me explica", "me fala", "me diz", "me manda",
    ]
    for p in nao_eh_nome_inicio:
        if t_lower.startswith(p):
            return ""
    
    # Frases que contêm palavras-chave de conversa = NÃO é nome
    nao_eh_nome_contem = [
        "crédito", "credito", "empréstimo", "emprestimo", "financiamento",
        "cartão", "cartao", "banco", "taxa", "juros", "parcela",
        "negativado", "negativação", "score", "serasa", "spc",
        "veículo", "veiculo", "imóvel", "imovel", "consignado",
        "saber", "informação", "informacao", "dúvida", "duvida",
        "reunião", "reuniao", "agendar", "marcar", "horário", "horario",
        "ajuda", "resolver", "problema", "situação", "situacao",
        "obrigado", "obrigada", "valeu", "falou",
    ]
    for p in nao_eh_nome_contem:
        if p in t_lower:
            return ""
    
    # ── LIMPEZA DE SAUDAÇÕES ──────────────────────────────────────────
    saudacoes_inicio = [
        r'^(?:oi|olá|ola|hey|opa|eai|e ai|iae|bom dia|boa tarde|boa noite|oii+)\s*[,!.]?\s*',
    ]
    for pat in saudacoes_inicio:
        t = re.sub(pat, '', t, flags=re.IGNORECASE).strip()
    
    # ── LIMPEZA DE EXPRESSÕES DE APRESENTAÇÃO ─────────────────────────
    expressoes = [
        r'(?:pode\s+)?me\s+chamar?\s+de\s+',
        r'meu\s+nome\s+(?:é|e|eh)\s+',
        r'me\s+chamo\s+',
        r'(?:tá|ta|está|esta)\s+(?:falando|falano)\s+com\s+(?:o|a)?\s*',
        r'(?:fala|falando)\s+com\s+(?:o|a)?\s*',
        r'quem\s+fala\s+(?:é|e|eh)\s+',
        r'aqui\s+(?:é|e|eh)\s+(?:o|a)?\s*',
        r'(?:é|e|eh)\s+(?:o|a)\s+',
        r'eu\s+sou\s+(?:o|a)\s+',
        r'eu\s+sou\s+',
        r'sou\s+(?:o|a)\s+',
        r'sou\s+',
        r'com\s+(?:o|a)\s+',
        r'com\s+',
    ]
    for pat in expressoes:
        t = re.sub(r'^' + pat, '', t, flags=re.IGNORECASE).strip()
    
    # ── CORTA FRASES DEPOIS DO NOME ───────────────────────────────────
    cortes = [
        r'\s+e\s+(?:queria|gostaria|preciso|quero|tenho|estou|tô|to)\b.*',
        r'\s+(?:queria|gostaria|preciso|quero|tenho|estou)\b.*',
        r'\s*[,]\s+.*',
        r'\s+tudo\s+bem.*',
        r'\s+como\s+vai.*',
    ]
    for pat in cortes:
        t = re.sub(pat, '', t, flags=re.IGNORECASE).strip()
    
    # Remove números
    if re.search(r'\d', t):
        t = re.sub(r'\d+', '', t).strip()
    
    # Se sobrou vazio, retorna vazio
    if not t:
        return ""
    
    # ── VALIDAÇÃO FINAL ───────────────────────────────────────────────
    # Se ainda contém verbos comuns, provavelmente não é nome
    palavras_proibidas = [
        "quero", "queria", "preciso", "gostaria", "sobre", "como",
        "saber", "fazer", "falar", "ver", "pedir", "ter",
    ]
    for p in palavras_proibidas:
        if p in t.lower().split():
            return ""
    
    return t.strip()


def _formatar_data_br(data_hora_str: str) -> str:
    try:
        dt = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
        dias = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
        return f"{dias[dt.weekday()]}, {dt.strftime('%d/%m')} às {dt.strftime('%H:%M')}h"
    except:
        return data_hora_str


def _detectar_dia_sem_hora(mensagem: str, agora_ref=None) -> str | None:
    """Detecta se o cliente mencionou um dia sem especificar hora."""
    import re
    from datetime import timezone, timedelta
    agora = agora_ref or datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)
    texto = mensagem.lower().strip()
    dias_semana = {'segunda':0,'terca':1,'terça':1,'quarta':2,'quinta':3,'sexta':4,'sabado':5,'sábado':5,'domingo':6}

    data_alvo = None
    if 'hoje' in texto:
        data_alvo = agora
    elif any(w in texto for w in ['amanhã','amanha']):
        data_alvo = agora + timedelta(days=1)
    else:
        m = re.search(r'(\d{1,2})/(\d{1,2})', texto)
        if m:
            try:
                data_alvo = datetime(agora.year, int(m.group(2)), int(m.group(1)))
            except: pass
        if data_alvo is None:
            for nome, wd in dias_semana.items():
                if nome in texto:
                    diff = (wd - agora.weekday()) % 7
                    data_alvo = agora + timedelta(days=diff)
                    break

    if data_alvo is None:
        return None
    return data_alvo.strftime("%Y-%m-%d 08:00")


def _gpt_interpretar_horario(mensagem: str) -> str | None:
    """Interpretação determinística de datas em português brasileiro."""
    import re
    from datetime import timezone, timedelta

    agora = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)
    texto = mensagem.lower().strip()

    hora_val = None
    for pat in [
        r'(\d{1,2})h(\d{2})',
        r'(\d{1,2}):(\d{2})',
        r'(\d{1,2})\s*h(?:oras?)?',
        r'às\s*(\d{1,2})(?:\D|$)',
        r'as\s*(\d{1,2})(?:\D|$)',
    ]:
        m = re.search(pat, texto)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2)) if len(m.groups()) > 1 and m.group(2) else 0
            if 0 <= h <= 23:
                hora_val = (h, mi)
                break

    if hora_val is None:
        if any(w in texto for w in ['manhã', 'manha', 'cedo']):
            hora_val = (9, 0)
        elif any(w in texto for w in ['tarde']):
            hora_val = (14, 0)
        elif any(w in texto for w in ['fim do dia', 'fim de dia', 'final do dia']):
            hora_val = (17, 0)

    dias_semana = {
        'segunda': 0, 'terca': 1, 'terça': 1,
        'quarta': 2, 'quinta': 3, 'sexta': 4,
        'sabado': 5, 'sábado': 5, 'domingo': 6,
    }

    data_alvo = None

    if 'hoje' in texto:
        data_alvo = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    elif any(w in texto for w in ['amanhã', 'amanha']):
        data_alvo = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif any(w in texto for w in ['depois de amanhã', 'depois de amanha']):
        data_alvo = (agora + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        m = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', texto)
        if m:
            dia = int(m.group(1))
            mes = int(m.group(2))
            ano = agora.year
            if m.group(3):
                ano_raw = int(m.group(3))
                ano = ano_raw if ano_raw > 100 else 2000 + ano_raw
            try:
                data_alvo = datetime(ano, mes, dia)
            except:
                pass

    if data_alvo is None:
        for nome, wd in dias_semana.items():
            if nome in texto:
                hoje_wd = agora.weekday()
                diff = (wd - hoje_wd) % 7
                if diff == 0:
                    data_alvo = agora.replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    data_alvo = (agora + timedelta(days=diff)).replace(hour=0, minute=0, second=0, microsecond=0)
                break

    if data_alvo is None or hora_val is None:
        return None

    h, mi = hora_val
    resultado = data_alvo.replace(hour=h, minute=mi, second=0, microsecond=0)

    # Se o horário já passou, avança para o próximo dia
    if resultado <= agora:
        resultado += timedelta(days=1)

    # ✅ CORREÇÃO: SEMPRE pula fim de semana, não só quando já passou
    while resultado.weekday() >= 5:
        resultado += timedelta(days=1)

    return resultado.strftime("%Y-%m-%d %H:%M")


def _gpt_intencao(mensagem: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resposta = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "Você analisa mensagens de clientes de uma financeira.\n"
                "Responda APENAS com uma dessas palavras:\n"
                "- agendar (cliente quer marcar reunião)\n"
                "- produtos (cliente quer saber sobre crédito/produtos)\n"
                "- outro (saudação, dúvida geral, ou fora do contexto)"
            )},
            {"role": "user", "content": mensagem}
        ],
        max_tokens=10, temperature=0
    )
    return resposta.choices[0].message.content.strip().lower()


def _horario_ocupado(data_hora_str: str, db: Session) -> bool:
    """Verifica se há reunião exatamente nesse horário."""
    try:
        dt = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
    except:
        return False
    dt_norm = dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)
    slot_str = dt_norm.strftime("%Y-%m-%d %H:%M")
    return db.query(Meeting).filter(
        Meeting.scheduled_at == slot_str,
        Meeting.status.in_(["agendado", "em_andamento"])
    ).count() > 0


def _normalizar_slot(dt: "datetime") -> "datetime":
    """Encaixa no slot de 30 min mais próximo."""
    from datetime import timedelta
    if dt.minute < 15:
        return dt.replace(minute=0, second=0, microsecond=0)
    elif dt.minute < 45:
        return dt.replace(minute=30, second=0, microsecond=0)
    else:
        return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _proximo_dia_util(dt: "datetime") -> "datetime":
    from datetime import timedelta
    d = dt + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _slots_do_dia(data: "datetime", db: Session) -> list:
    from datetime import timedelta, timezone
    agora = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)
    slots = []
    inicio = data.replace(hour=8, minute=30, second=0, microsecond=0)
    fim    = data.replace(hour=17, minute=30, second=0, microsecond=0)
    tentativa = inicio
    while tentativa <= fim:
        if tentativa > agora:
            slot_str = tentativa.strftime("%Y-%m-%d %H:%M")
            if not _horario_ocupado(slot_str, db):
                dia_nome = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"][tentativa.weekday()]
                slots.append({
                    "valor": slot_str,
                    "label": f"{tentativa.strftime('%d/%m')} ({dia_nome}) às {tentativa.strftime('%Hh%M').replace('h00','h')}"
                })
        tentativa += timedelta(minutes=30)
    return slots


def _slots_distribuidos(data: "datetime", db: Session) -> list:
    from datetime import timedelta, timezone
    agora = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)
    FAIXAS = [
        (8, 30, 11, 0,  "manhã"),
        (11, 0, 13, 0,  "meio-dia"),
        (13, 0, 17, 31, "tarde"),
    ]
    slots = []
    for h_ini, m_ini, h_fim, m_fim, _ in FAIXAS:
        inicio = data.replace(hour=h_ini, minute=m_ini, second=0, microsecond=0)
        fim    = data.replace(hour=h_fim, minute=m_fim,  second=0, microsecond=0)
        t = inicio
        while t <= fim:
            if t > agora:
                slot_str = t.strftime("%Y-%m-%d %H:%M")
                if not _horario_ocupado(slot_str, db):
                    dia_nome = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"][t.weekday()]
                    slots.append({
                        "valor": slot_str,
                        "label": f"{t.strftime('%d/%m')} ({dia_nome}) às {t.strftime('%Hh%M').replace('h00','h')}"
                    })
                    break
            t += timedelta(minutes=30)
    return slots


def _proximos_horarios_livres(data_hora_str: str, db: Session, quantidade: int = 3) -> list:
    from datetime import timedelta, timezone
    try:
        base = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
    except:
        base = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)
    livres = []
    tentativa = base.replace(hour=8, minute=30, second=0, microsecond=0)
    limite = base + timedelta(days=14)
    while len(livres) < quantidade and tentativa < limite:
        dia_util = tentativa.weekday() < 5
        dentro   = (tentativa.hour > 8 or (tentativa.hour == 8 and tentativa.minute >= 30)) and tentativa.hour < 18
        if dia_util and dentro:
            slot_str = tentativa.strftime("%Y-%m-%d %H:%M")
            if not _horario_ocupado(slot_str, db):
                dia_nome = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"][tentativa.weekday()]
                livres.append(f"{tentativa.strftime('%d/%m')} ({dia_nome}) às {tentativa.strftime('%Hh%M').replace('h00','h')}")
        tentativa += timedelta(minutes=30)
    return livres


# ── IA PRINCIPAL ─────────────────────────────────────────────────────────────────

# Carrega base de conhecimento dos arquivos .txt em prompts/
from prompts.loader import carregar_conhecimento

# Regras específicas do WhatsApp (não existem nos .txt porque são do canal)
WPP_REGRAS = f"""
IDENTIDADE OBRIGATÓRIA:
- Seu nome é {IA_NAME}
- Você é consultora da {EMPRESA_NOME}
- SEMPRE se apresente como "{IA_NAME} da {EMPRESA_NOME}"

REGRAS DO WHATSAPP (específicas deste canal):
- Use no máximo 1 emoji por mensagem
- Máximo 2 frases por resposta — seja direta
- NUNCA use markdown [texto](link) — escreva URLs limpos

FILOSOFIA: EXPLIQUE PRIMEIRO, AGENDE DEPOIS
- Quando o cliente não atendeu a ligação e respondeu no WhatsApp, ele quer ENTENDER.
- Primeiro explique como a {EMPRESA_NOME} funciona, tire dúvidas, responda sobre produtos.
- Só sugira reunião com especialista quando o cliente DEMONSTRAR INTERESSE em avançar.
- Se o cliente disser "sim" a "Posso te explicar?", EXPLIQUE — não pule pro agendamento.

REGRAS SOBRE TAXAS E CONDIÇÕES (CRÍTICO):
- NUNCA invente taxa, prazo ou valor que não esteja na base de conhecimento
- Se o produto tiver taxa definida, informe como "a partir de X%, mas depende da análise"
- Se o produto NÃO tiver taxa fixa, diga que "as condições dependem da análise do caso"
- SEMPRE finalize com: "a condição final depende da análise do perfil e da documentação"
- Na dúvida, use: "O especialista consegue passar a faixa mais adequada no seu caso"

REGRAS DE AGENDAMENTO (CRÍTICO):
- NUNCA diga "vou agendar", "agendei", "está confirmado", "marcado para"
- NUNCA pergunte dia ou horário — o SISTEMA cuida disso automaticamente
- NUNCA invente horários disponíveis — o SISTEMA consulta a agenda real
- Quando o cliente quiser agendar, o sistema assume e cuida de tudo

FLUXO 1 — CLIENTE INTERESSADO (já conversou na ligação):
Ele já sabe quem você é. Não reapresente a empresa. Responda dúvidas.

FLUXO 2 — CLIENTE NÃO ATENDEU A LIGAÇÃO:
Você tentou ligar e não conseguiu. Ele respondeu no WhatsApp.
EXPLIQUE o motivo da ligação. Tire dúvidas. Só sugira reunião depois que ele entender.

FLUXO 3 — CLIENTE EXTERNO (veio por conta própria):
Você não sabe quem é. Seja acolhedora. Entenda o que ele precisa.
Separe a trilha cedo: "Hoje você busca algo para empresa ou para pessoa física?"
"""

def _get_ia_system() -> str:
    """Monta o system prompt completo: base de conhecimento + regras do WhatsApp + dados institucionais."""
    from integrations.whatsapp import DADOS_INSTITUCIONAIS
    
    base = carregar_conhecimento()
    
    # Adiciona dados institucionais ao contexto
    d = DADOS_INSTITUCIONAIS
    inst = "\n\nDADOS INSTITUCIONAIS DA EMPRESA (use quando o cliente perguntar sobre confiabilidade):\n"
    inst += f"Empresa: {d['nome_empresa']}\n"
    if d["cnpj"]:
        inst += f"CNPJ: {d['cnpj']}\n"
    if d["site"]:
        inst += f"Site: {d['site']}\n"
    if d["instagram"]:
        inst += f"Instagram: {d['instagram']}\n"
    inst += "REGRA: NUNCA invente dados institucionais. Use APENAS os dados acima."
    
    return base + "\n\n" + WPP_REGRAS + inst


def _gerar_resposta_ia(historico: list, ctx: str = "", lead=None) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # ✅ CORREÇÃO: Monta memória completa do lead — inclui resumo E transcrição
    memoria = ""
    if lead:
        partes = []
        if getattr(lead, "name", "") and lead.name.strip():
            partes.append(f"Nome do cliente: {lead.name.strip()}")
        if getattr(lead, "stage", ""):
            partes.append(f"Stage: {lead.stage}")
        if getattr(lead, "product", "") and lead.product:
            partes.append(f"Produto de interesse: {lead.product}")
        if getattr(lead, "desired_value", "") and lead.desired_value:
            partes.append(f"Valor desejado: {lead.desired_value}")
        if getattr(lead, "urgency", "") and lead.urgency:
            partes.append(f"Urgência: {lead.urgency}")
        if getattr(lead, "temperature", "") and lead.temperature:
            partes.append(f"Temperatura: {lead.temperature}")

        # ✅ NOVO: inclui resumo da ligação (crucial para continuidade)
        resumo = getattr(lead, "resumo", "") or getattr(lead, "ai_summary", "")
        if resumo:
            partes.append(f"Resumo da ligação anterior: {resumo}")

        # ✅ NOVO: inclui trecho da transcrição se existir (últimas falas)
        conversa = getattr(lead, "conversa", "")
        if conversa and conversa.strip():
            linhas = conversa.strip().split("\n")
            ultimas = linhas[-10:] if len(linhas) > 10 else linhas
            partes.append(
                "Trecho da conversa na ligação (para contexto, NÃO repita o que já foi dito):\n"
                + "\n".join(ultimas)
            )

        if partes:
            memoria = (
                "MEMÓRIA DO LEAD (use para dar continuidade, NÃO se reapresente, "
                "NÃO repita informações que o cliente já ouviu):\n"
                + "\n".join(partes)
            )

    system = _get_ia_system()
    if memoria:
        system += f"\n\n{memoria}"
    if ctx:
        system += f"\n\nINSTRUÇÃO: {ctx}"

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}] + historico[-20:],
        max_tokens=200, temperature=0.7
    )
    return resp.choices[0].message.content.strip()


def _atualizar_lead_ia(lead, historico: list, db) -> None:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    conv = "\n".join([f"{'Cliente' if m['role']=='user' else 'Julia'}: {m['content']}" for m in historico[-10:]])
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":(
                f"Analise e responda em JSON puro sem markdown:\n{conv}\n\n"
                f'{{"produto":"nome ou vazio","temperatura":"hot|warm|cold","resumo":"1 frase"}}'
            )}],
            max_tokens=80, temperature=0
        )
        data = json.loads(resp.choices[0].message.content.strip())
        if data.get("produto"):     lead.product     = data["produto"]
        if data.get("temperatura"): lead.temperature = data["temperatura"]
        if data.get("resumo"):      lead.ai_summary  = data["resumo"]
        db.commit()
    except: pass


def _todos_slots_do_dia(data: "datetime", db: "Session") -> list:
    from datetime import timedelta, timezone
    agora = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)
    slots = []
    t = data.replace(hour=8, minute=30, second=0, microsecond=0)
    fim = data.replace(hour=17, minute=30, second=0, microsecond=0)
    while t <= fim:
        if t > agora:
            slot_str = t.strftime("%Y-%m-%d %H:%M")
            if not _horario_ocupado(slot_str, db):
                dia_nome = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"][t.weekday()]
                hora_fmt = t.strftime("%Hh%M").replace("h00","h")
                slots.append({
                    "valor": slot_str,
                    "label": f"{t.strftime('%d/%m')} ({dia_nome}) às {hora_fmt}"
                })
        t += timedelta(minutes=30)
    return slots


# ─── FUNÇÕES DE MENU NUMÉRICO ────────────────────────────────────────────────

NOMES_DIA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def _gerar_datas_disponiveis(agora: "datetime", semana_offset: int = 0) -> list:
    """Gera as datas disponíveis (seg-sex) a partir de amanhã + offset de semanas."""
    from datetime import timedelta
    datas = []
    inicio = agora + timedelta(days=1)
    inicio += timedelta(weeks=semana_offset)
    while inicio.weekday() >= 5:
        inicio += timedelta(days=1)
    dt = inicio
    while len(datas) < 5:
        if dt.weekday() < 5:
            datas.append({
                "id": dt.strftime("%Y-%m-%d"),
                "label": f"{NOMES_DIA[dt.weekday()]} • {dt.strftime('%d/%m')}"
            })
        dt += timedelta(days=1)
    return datas


def _enviar_selecao_tipo(numero: str, lead_id: str, estado: dict, db, msg_intro: str = ""):
    """Envia menu numérico: Vídeo, Ligação ou WhatsApp."""
    corpo = msg_intro or "Como prefere a reunião com nosso especialista?"
    msg = (
        corpo + "\n\n"
        "1️⃣ Vídeo Chamada 🎥\n"
        "2️⃣ Ligação Telefônica 📞\n"
        "3️⃣ Conversa por WhatsApp 💬\n\n"
        "Responda com o número da opção."
    )
    _responder(numero, msg, lead_id, estado, db)


def _enviar_selecao_dia(numero: str, lead_id: str, estado: dict, db,
                        semana_offset: int = 0, msg_intro: str = ""):
    """Envia menu numérico com datas disponíveis."""
    from datetime import timezone, timedelta
    agora = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)

    datas = _gerar_datas_disponiveis(agora, semana_offset)
    estado["semana_offset"] = semana_offset
    estado["datas_disponiveis"] = datas

    corpo = msg_intro or "Escolha o melhor dia para a reunião:"

    linhas = []
    for i, d in enumerate(datas, 1):
        linhas.append(f"{i}️⃣ 📅 {d['label']}")
    linhas.append(f"{len(datas)+1}️⃣ ➡️ Ver mais datas")

    msg = corpo + "\n\n" + "\n".join(linhas) + "\n\nResponda com o número da opção."
    _responder(numero, msg, lead_id, estado, db)


def _enviar_selecao_horario(numero: str, lead_id: str, estado: dict, db,
                            dia_str: str, slots: list, msg_intro: str = ""):
    """Envia menu numérico com horários disponíveis (já filtrados por turno)."""
    dt_dia = datetime.strptime(dia_str, "%Y-%m-%d")
    dia_nome = NOMES_DIA[dt_dia.weekday()]

    turno = estado.get("turno", "")
    turno_txt = "de manhã ☀️" if turno == "manha" else "à tarde 🌤️" if turno == "tarde" else ""
    corpo = msg_intro or f"Horários {turno_txt} para {dia_nome}, {dt_dia.strftime('%d/%m')}:"

    linhas = []
    for i, s in enumerate(slots[:10], 1):
        hora_fmt = s["valor"].split(" ")[1][:5]
        linhas.append(f"{i}️⃣ 🕐 {hora_fmt}")

    if not linhas:
        _responder(numero, "Sem horários disponíveis nesse turno. 😔 Escolha outro!",
                   lead_id, estado, db)
        return False

    msg = corpo + "\n\n" + "\n".join(linhas) + "\n\nResponda com o número da opção."
    _responder(numero, msg, lead_id, estado, db)
    return True


def _enviar_selecao_turno(numero: str, lead_id: str, estado: dict, db,
                          dia_str: str, msg_intro: str = ""):
    """Envia menu numérico: Manhã ou Tarde."""
    dt_dia = datetime.strptime(dia_str, "%Y-%m-%d")
    dia_nome = NOMES_DIA[dt_dia.weekday()]

    corpo = msg_intro or f"Ótimo! {dia_nome}, {dt_dia.strftime('%d/%m')}. Prefere qual turno?"
    msg = (
        corpo + "\n\n"
        "1️⃣ ☀️ Manhã (08:30 às 11:30)\n"
        "2️⃣ 🌤️ Tarde (13:30 às 17:30)\n\n"
        "Responda com o número da opção."
    )
    _responder(numero, msg, lead_id, estado, db)


def _filtrar_slots_por_turno(slots: list, turno: str) -> list:
    """Filtra slots por turno: manhã (08:30-11:30) ou tarde (13:30-17:30)."""
    filtrados = []
    for s in slots:
        hora_str = s["valor"].split(" ")[1][:5]
        partes = hora_str.split(":")
        hora = int(partes[0])
        minuto = int(partes[1])
        total_min = hora * 60 + minuto
        if turno == "manha" and total_min <= 11 * 60 + 30:  # até 11:30
            filtrados.append(s)
        elif turno == "tarde" and total_min >= 13 * 60 + 30:  # a partir de 13:30
            filtrados.append(s)
    return filtrados


def _enviar_confirmacao(numero: str, lead_id: str, estado: dict, db, data_hora: str):
    """Envia mensagem de confirmação com opções numéricas."""
    data_br = _formatar_data_br(data_hora)
    dt = datetime.strptime(data_hora, "%Y-%m-%d %H:%M")
    dia_nome = NOMES_DIA[dt.weekday()]
    tipo_r = estado.get("tipo_reuniao") or ("video_chamada" if estado.get("quer_meet") else "ligacao")
    tipo_map = {"video_chamada": "🎥 Vídeo Chamada", "ligacao": "📞 Ligação", "whatsapp": "💬 WhatsApp"}
    tipo_txt = tipo_map.get(tipo_r, "📞 Ligação")

    msg = (
        f"Perfeito! Sua reunião ficou assim:\n\n"
        f"📅 {dia_nome} • {dt.strftime('%d/%m')}\n"
        f"🕐 {dt.strftime('%H:%M')}\n"
        f"{tipo_txt}\n\n"
        f"1️⃣ ✅ Confirmar reunião\n"
        f"2️⃣ 🔄 Escolher outro horário\n\n"
        f"Responda com o número da opção."
    )
    _responder(numero, msg, lead_id, estado, db)


# ── WEBHOOK ──────────────────────────────────────────────────────────────────

# ── TRANSCRIÇÃO DE ÁUDIO ──────────────────────────────────────────────────────

async def _transcrever_audio(body: dict, data: dict) -> str:
    """
    Tenta transcrever áudio recebido no WhatsApp.
    
    Fluxo:
    1. Tenta pegar base64 direto do webhook (Evolution API v2+)
    2. Se não tiver, tenta baixar via API da Evolution
    3. Envia para OpenAI Whisper para transcrição
    """
    import base64
    import tempfile
    import httpx

    msg = data.get("message", {})
    audio_msg = msg.get("audioMessage") or msg.get("pttMessage") or {}

    audio_base64 = None
    
    # Método 1: base64 direto no webhook (Evolution API v2 com webhook base64 habilitado)
    if msg.get("base64"):
        audio_base64 = msg["base64"]
        print("🎤 Áudio obtido via base64 direto no webhook")

    # Método 2: Chamar API da Evolution para obter base64
    if not audio_base64:
        try:
            instance = os.getenv("EVOLUTION_INSTANCE", "sofia")
            evolution_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
            evolution_key = os.getenv("EVOLUTION_API_KEY", "")
            
            if evolution_url and evolution_key:
                message_key = data.get("key", {})
                endpoint = f"{evolution_url}/chat/getBase64FromMediaMessage/{instance}"
                
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        endpoint,
                        json={"message": {"key": message_key, "message": msg}},
                        headers={"apikey": evolution_key}
                    )
                    
                    if resp.status_code == 200:
                        result = resp.json()
                        audio_base64 = result.get("base64", "")
                        if audio_base64:
                            print("🎤 Áudio obtido via Evolution API getBase64")
        except Exception as e:
            print(f"⚠️ Erro ao obter áudio via Evolution API: {e}")

    if not audio_base64:
        print("⚠️ Não foi possível obter o áudio")
        return ""

    # Decodifica e salva em arquivo temporário
    try:
        audio_bytes = base64.b64decode(audio_base64)
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        # Transcreve com Whisper
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        with open(tmp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="pt"
            )
        
        # Limpa arquivo temporário
        try:
            os.unlink(tmp_path)
        except:
            pass

        texto = transcription.text.strip()
        return texto if texto else ""

    except Exception as e:
        print(f"⚠️ Erro na transcrição do áudio: {e}")
        import traceback
        traceback.print_exc()
        return ""


# ── WEBHOOK ───────────────────────────────────────────────────────────────────

WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "sofia-verify-token")


@router.get("/webhook")
async def whatsapp_verify(request: Request):
    """Verificação de webhook exigida pela Meta Cloud API."""
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        print(f"✅ Webhook verificado pela Meta")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(challenge)
    print(f"❌ Verificação falhou — token: {token}")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("Forbidden", status_code=403)


def _extrair_msg_meta(body: dict):
    """Extrai número e conteúdo de uma mensagem recebida via Meta Cloud API."""
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None, None, None
        msg = messages[0]
        numero = msg.get("from", "")
        msg_type = msg.get("type", "")
        texto = ""
        if msg_type == "text":
            texto = msg.get("text", {}).get("body", "").strip()
        elif msg_type == "image":
            texto = msg.get("image", {}).get("caption", "").strip()
        elif msg_type == "button":
            texto = msg.get("button", {}).get("text", "").strip()
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            itype = interactive.get("type", "")
            if itype == "button_reply":
                texto = interactive.get("button_reply", {}).get("title", "").strip()
            elif itype == "list_reply":
                texto = interactive.get("list_reply", {}).get("title", "").strip()
        elif msg_type in ("audio", "voice"):
            texto = ""  # será tratado como áudio
        return numero, msg_type, texto
    except Exception as e:
        print(f"⚠️ Erro ao extrair mensagem Meta: {e}")
        return None, None, None


@router.post("/webhook")
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()

        # ── META CLOUD API FORMAT ────────────────────────────────────────────
        if body.get("object") == "whatsapp_business_account":
            # Statuses (sent/delivered/read) — processar ANTES de extrair mensagem
            entry = body.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            value = changes.get("value", {})

            # ✅ Filtro por phone_number_id — evita receber msgs de outro número da mesma WABA
            phone_id_webhook = value.get("metadata", {}).get("phone_number_id", "")
            phone_id_esperado = os.getenv("WA_PHONE_NUMBER_ID", "")
            if phone_id_esperado and phone_id_webhook and phone_id_webhook != phone_id_esperado:
                print(f"🚫 Webhook ignorado — phone_number_id {phone_id_webhook} não é deste projeto ({phone_id_esperado})")
                return {"ok": True}

            if value.get("statuses") and not value.get("messages"):
                for st in value.get("statuses", []):
                    wamid = st.get("id", "")
                    new_status = st.get("status", "")  # sent, delivered, read, failed
                    print(f"📨 Status webhook: {new_status} para wamid={wamid[:30]}")
                    if wamid and new_status in ("sent", "delivered", "read"):
                        msg = db.query(WppMensagem).filter(WppMensagem.wamid == wamid).first()
                        if msg:
                            msg.status = new_status
                            db.commit()
                            print(f"   ✅ Atualizado para {new_status}")
                        else:
                            print(f"   ⚠️ Mensagem não encontrada no banco")
                return {"ok": True}

            # Extrair mensagem
            numero_raw, msg_type, texto = _extrair_msg_meta(body)
            if not numero_raw:
                return {"ok": True}

            numero = _normalizar_numero(numero_raw)

            # Áudio sem texto — pede para digitar
            if msg_type in ("audio", "voice") and not texto:
                print("🎤 Áudio recebido via Meta — sem transcrição local disponível")
                lead = _buscar_lead_por_numero(numero, db)
                if lead:
                    estado = _get_estado(lead, db)
                    _responder(numero,
                        "Recebi seu áudio! 🎤 Infelizmente não consigo ouvir áudios ainda. "
                        "Pode me enviar por texto, por favor?",
                        lead.id, estado, db)
                    _save_estado(lead, estado, db)
                return {"ok": True}

            # Imagem sem legenda
            if msg_type == "image" and not texto:
                print("📸 Imagem sem legenda recebida via Meta")
                lead = _buscar_lead_por_numero(numero, db)
                if lead:
                    estado = _get_estado(lead, db)
                    _responder(numero,
                        "Recebi sua imagem! 📸 Se precisar me dizer algo, "
                        "pode enviar por texto que eu consigo entender melhor.",
                        lead.id, estado, db)
                    _save_estado(lead, estado, db)
                return {"ok": True}

            # Tipos sem texto (documento, sticker, vídeo, location, etc)
            if not texto:
                print(f"📎 Tipo {msg_type} sem texto — ignorando")
                return {"ok": True}

            print(f"📱 WhatsApp Meta de {numero}: {texto}")

            lead = _buscar_lead_por_numero(numero, db)
            if not lead:
                import uuid
                lead = Lead(
                    id=str(uuid.uuid4()),
                    phone=numero,
                    name="",
                    stage="novo",
                    temperature="pending",
                    wpp_etapa="aguardando_nome",
                    wpp_pendente="",
                    wpp_dia_ref="",
                    wpp_quer_meet="1",
                    conversa_estado="[]",
                )
                db.add(lead)
                db.commit()
                db.refresh(lead)
                print(f"✨ Novo lead criado: {numero}")

            estado = _get_estado(lead, db)
            await _processar(texto, numero, lead, estado, db)
            _save_estado(lead, estado, db)
            return {"ok": True}

        # ── EVOLUTION API FORMAT (legado / fallback) ─────────────────────────
        if body.get("event","") != "messages.upsert":
            return {"ok": True}
        data   = body.get("data",{})
        msg    = data.get("message",{})
        jid    = data.get("key",{}).get("remoteJid","")
        fromMe = data.get("key",{}).get("fromMe", False)
        if fromMe: return {"ok": True}

        texto  = (msg.get("conversation") or
                  msg.get("extendedTextMessage",{}).get("text","") or "").strip()

        if not texto:
            btn_resp = msg.get("buttonsResponseMessage", {})
            if btn_resp:
                texto = btn_resp.get("selectedButtonId", "")

        if not texto:
            list_resp = msg.get("listResponseMessage", {})
            if list_resp:
                texto = list_resp.get("singleSelectReply", {}).get("selectedRowId", "")
                if not texto:
                    texto = list_resp.get("title", "")

        if not texto:
            tmpl_resp = msg.get("templateButtonReplyMessage", {})
            if tmpl_resp:
                texto = tmpl_resp.get("selectedId", "")

        if not texto and (msg.get("audioMessage") or msg.get("pttMessage")):
            print("🎤 Mensagem de áudio recebida — tentando transcrever...")
            texto = await _transcrever_audio(body, data)
            if texto:
                print(f"🎤 Transcrição: {texto}")
            else:
                numero = jid.replace("@s.whatsapp.net","").replace("@c.us","")
                numero = _normalizar_numero(numero)
                lead = _buscar_lead_por_numero(numero, db)
                if lead:
                    estado = _get_estado(lead, db)
                    _responder(numero,
                        "Recebi seu áudio! 🎤 Infelizmente não consegui ouvir direito. "
                        "Pode me enviar por texto, por favor?",
                        lead.id, estado, db)
                    _save_estado(lead, estado, db)
                return {"ok": True}

        if not texto and msg.get("imageMessage"):
            caption = msg.get("imageMessage", {}).get("caption", "")
            if caption:
                texto = caption
            else:
                numero = jid.replace("@s.whatsapp.net","").replace("@c.us","")
                numero = _normalizar_numero(numero)
                lead = _buscar_lead_por_numero(numero, db)
                if lead:
                    estado = _get_estado(lead, db)
                    _responder(numero,
                        "Recebi sua imagem! 📸 Se precisar me dizer algo, "
                        "pode enviar por texto que eu consigo entender melhor.",
                        lead.id, estado, db)
                    _save_estado(lead, estado, db)
                return {"ok": True}

        if not texto and (msg.get("documentMessage") or msg.get("stickerMessage") or msg.get("videoMessage")):
            return {"ok": True}

        if not texto: return {"ok": True}

        numero = jid.replace("@s.whatsapp.net","").replace("@c.us","")
        numero = _normalizar_numero(numero)
        print(f"📱 WhatsApp de {numero}: {texto}")

        lead = _buscar_lead_por_numero(numero, db)
        if not lead:
            import uuid
            lead = Lead(
                id=str(uuid.uuid4()),
                phone=numero,
                name="",
                stage="novo",
                temperature="pending",
                wpp_etapa="aguardando_nome",
                wpp_pendente="",
                wpp_dia_ref="",
                wpp_quer_meet="1",
                conversa_estado="[]",
            )
            db.add(lead)
            db.commit()
            db.refresh(lead)
            print(f"✨ Novo lead criado: {numero}")

        estado = _get_estado(lead, db)
        await _processar(texto, numero, lead, estado, db)
        _save_estado(lead, estado, db)
        return {"ok": True}
    except Exception as e:
        print(f"❌ Webhook erro: {e}")
        import traceback; traceback.print_exc()
        return {"ok": True}


# ── PROCESSAMENTO ─────────────────────────────────────────────────────────────

async def _processar(texto: str, numero: str, lead, estado: dict, db):
    from datetime import timezone, timedelta
    agora_br = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None)

    if "historico" not in estado:
        estado["historico"] = []

    # ── Salva mensagem do cliente no banco IMEDIATAMENTE ─────────────────────
    _salvar_msg(lead.id, "user", texto, db)
    estado["historico"].append({"role": "user", "content": texto})

    # ── IA PAUSADA — salva msg mas não responde ──────────────────────────
    if getattr(lead, "ia_pausada", False):
        _save_estado(lead, estado, db)
        print(f"⏸️ IA pausada para {lead.name} — mensagem salva, sem resposta automática")
        return

    stage = lead.stage or "novo"
    etapa = estado.get("etapa", "conversa")
    nome  = lead.name or ""

    # ─────────────────────────────────────────────────────────────────────────
    # ETAPA: AGUARDANDO NOME
    # ─────────────────────────────────────────────────────────────────────────
    if etapa == "aguardando_nome":
        if len(estado["historico"]) == 1:
            msg_ia = (
                f"Olá! 👋 Aqui é a {IA_NAME} da {EMPRESA_NOME}. "
                "Com quem tenho o prazer de falar?"
            )
            _salvar_msg(lead.id, "assistant", msg_ia, db)
            estado["historico"].insert(0, {"role": "assistant", "content": msg_ia})
            _enviar(numero, msg_ia)
            _save_estado(lead, estado, db)
            return
        else:
            # ── EXTRAÇÃO INTELIGENTE DE NOME ──────────────────────────────
            nome_extraido = _extrair_nome(texto)
            
            saudacoes = {"oi", "olá", "ola", "hey", "opa", "bom dia", "boa tarde", 
                         "boa noite", "oii", "oiii", "eai", "e ai", "iae"}
            
            # Se é saudação pura, pede o nome
            if not nome_extraido and texto.strip().lower() in saudacoes:
                resp = "Olá! 😊 Com quem tenho o prazer de falar?"
                _responder(numero, resp, lead.id, estado, db)
                _save_estado(lead, estado, db)
                return
            
            # Se extração retornou vazio = pessoa mandou uma frase, não um nome
            # Responde à frase COM pedido de nome
            if not nome_extraido or len(nome_extraido) <= 2:
                resp = _gerar_resposta_ia(
                    estado["historico"],
                    "O cliente respondeu algo que NÃO é um nome. "
                    "Ele pode ter feito uma pergunta ou comentário. "
                    "Responda BREVEMENTE ao que ele disse (1 frase curta) "
                    "e depois peça o nome dele de forma simpática. "
                    "Exemplo: 'Claro, posso te ajudar com isso! Mas antes, qual é seu nome? 😊'",
                    lead=lead
                )
                _responder(numero, resp, lead.id, estado, db)
                _save_estado(lead, estado, db)
                return
            
            # Se o nome extraído tem mais de 4 palavras, provavelmente é frase
            palavras = nome_extraido.split()
            if len(palavras) > 4:
                resp = (
                    "Entendi! 😊 Para eu te cadastrar certinho, "
                    "me diga apenas seu nome, por favor."
                )
                _responder(numero, resp, lead.id, estado, db)
                _save_estado(lead, estado, db)
                return
            
            # Nome ok — salva
            nome_final = nome_extraido.title()
            lead.name = nome_final
            nome = nome_final
            estado["etapa"] = "conversa"
            etapa = "conversa"
            db.commit()
            print(f"📝 Nome capturado: '{texto}' → '{nome_final}'")
            resp = _gerar_resposta_ia(
                estado["historico"],
                f"O cliente disse que se chama {nome_final}. "
                f"Cumprimente-o pelo nome, apresente-se como {IA_NAME} da {EMPRESA_NOME} "
                "e pergunte o que ele precisa. Máximo 2 frases.",
                lead=lead
            )
            _responder(numero, resp, lead.id, estado, db)
            _save_estado(lead, estado, db)
            return

    # ─────────────────────────────────────────────────────────────────────────
    # CONTEXTO INICIAL — injeta apenas na primeira mensagem do cliente
    # ─────────────────────────────────────────────────────────────────────────
    # ✅ CORREÇÃO: conta apenas mensagens do user para determinar se é a primeira
    user_msgs = [m for m in estado["historico"] if m["role"] == "user"]
    is_primeira_msg_user = len(user_msgs) == 1

    if is_primeira_msg_user:

        # FLUXO 1: INTERESSADO (já conversou na ligação)
        if stage == "interessado":
            # ✅ Verifica se já tem mensagem de abertura no histórico
            tem_abertura = any(
                "FLC Bank" in m.get("content", "") and m["role"] == "assistant"
                for m in estado["historico"]
            )
            if not tem_abertura:
                saudacao = (f"Olá {nome}! 😊 " if nome else "Olá! 😊 ")
                msg_ab = (
                    saudacao +
                    f"Aqui é a {IA_NAME} da {EMPRESA_NOME}. "
                    "Foi ótimo conversar com você! 😊\n\n"
                    "Vamos prosseguir para o agendamento ou ficou com alguma dúvida sobre o que conversamos? "
                    "Pode me perguntar aqui que eu te ajudo!"
                )
                estado["historico"].insert(-1, {"role": "assistant", "content": msg_ab})

            if etapa not in ("aguardando_tipo", "aguardando_dia", "aguardando_turno", "aguardando_horario", "confirmar", "concluido"):
                estado["etapa"] = "conversa"
                etapa = "conversa"

        # FLUXO 2: NÃO ATENDEU
        elif stage == "nao_atendeu":
            # ✅ A mensagem já deve estar no histórico (salva pelo routes_calls)
            tem_abertura = any(
                "FLC Bank" in m.get("content", "") and m["role"] == "assistant"
                for m in estado["historico"]
            )
            if not tem_abertura:
                saudacao = (f"Olá {nome}! 👋 " if nome else "Olá! 👋 ")
                msg_ab = (
                    saudacao +
                    f"Aqui é a {IA_NAME} da {EMPRESA_NOME}. "
                    "Tentei te ligar agora mas não consegui falar com você. "
                    "Liguei porque a gente é especializada em crédito para negativados e reestruturação financeira de empresas — "
                    "trabalhamos com mais de 60 instituições e conseguimos opções que banco tradicional não oferece. "
                    "Posso te explicar melhor como funciona?"
                )
                estado["historico"].insert(-1, {"role": "assistant", "content": msg_ab})

            if etapa not in ("aguardando_tipo", "aguardando_dia", "aguardando_turno", "aguardando_horario", "confirmar", "concluido"):
                estado["etapa"] = "pos_ligacao"
                etapa = "pos_ligacao"

        # AGENDADO
        elif stage == "agendado":
            reuniao = getattr(lead, "scheduled_at", None) or lead.agendado_hora or ""
            data_br = _formatar_data_br(reuniao) if reuniao else ""
            info = (f" para {data_br}") if data_br else ""
            saudacao = (f"Olá {nome}! 👋 " if nome else "Olá! 👋 ")
            msg_ab = (
                saudacao +
                f"Aqui é a {IA_NAME} da {EMPRESA_NOME}. "
                f"Sua reunião está confirmada{info}. "
                "Posso te ajudar com mais alguma coisa?"
            )
            estado["historico"].insert(-1, {"role": "assistant", "content": msg_ab})

        # CALLBACK AGENDADO
        elif stage == "callback_agendado":
            from db.database import Callback
            cb = (
                db.query(Callback)
                .filter(Callback.lead_id == lead.id, Callback.status == "pendente")
                .first()
            )
            if cb:
                hora_cb = cb.scheduled_at.strftime("%H:%M") if cb.scheduled_at else ""
                saudacao = (f"Oi {nome}! 😊 " if nome else "Oi! 😊 ")
                msg_ab = (
                    saudacao +
                    f"Aqui é a {IA_NAME} da {EMPRESA_NOME}. "
                    f"Conforme combinamos, tenho uma ligação marcada pra você às {hora_cb}.\n\n"
                    "Se quiser mudar o horário, é só me falar aqui! "
                    "Ou se preferir, podemos conversar por aqui mesmo pelo WhatsApp. 💬"
                )
            else:
                saudacao = (f"Oi {nome}! 😊 " if nome else "Oi! 😊 ")
                msg_ab = (
                    saudacao +
                    f"Aqui é a {IA_NAME} da {EMPRESA_NOME}. "
                    "Como posso te ajudar? 😊"
                )
            estado["historico"].insert(-1, {"role": "assistant", "content": msg_ab})
            if etapa not in ("aguardando_tipo", "aguardando_dia", "aguardando_turno", "aguardando_horario", "confirmar", "concluido"):
                estado["etapa"] = "conversa"
                etapa = "conversa"

        # FLUXO 3: EXTERNO
        else:
            saudacao = (f"Olá {nome}! 👋 " if nome else "Olá! 👋 ")
            msg_ab = (
                saudacao +
                f"Aqui é a {IA_NAME} da {EMPRESA_NOME}. "
                "A gente ajuda clientes que precisam de crédito, inclusive negativados, "
                "além de atuar com créditos para empresas e para pessoas físicas. "
                "Me conta o que você precisa? 😊"
            )
            estado["historico"].insert(-1, {"role": "assistant", "content": msg_ab})
            if etapa not in ("aguardando_tipo", "aguardando_dia", "aguardando_turno", "aguardando_horario", "confirmar", "concluido"):
                estado["etapa"] = "conversa"
                etapa = "conversa"

    # ─────────────────────────────────────────────────────────────────────────
    # FLUXO 2: PÓS LIGAÇÃO
    # ─────────────────────────────────────────────────────────────────────────
    if etapa == "pos_ligacao":
        t = texto.lower().strip()

        # Só vai pro agendamento se usar palavras/frases de agendamento
        quer_agendar_explicito = any(w in t for w in [
            "agendar", "reunião", "reuniao", "marcar reunião", "marcar reuniao",
            "quero marcar", "quero agendar", "vamos agendar", "vamos marcar",
            "bora marcar", "pode marcar", "marca pra mim",
            "falar com especialista", "falar com humano", "falar com atendente",
            "especialista",
        ])

        nao_quer = any(w in t for w in [
            "sem interesse", "não quero", "nao quero",
            "parar", "não me ligue", "nao me ligue", "sair",
            "para de", "não preciso", "nao preciso"
        ])

        if nao_quer:
            lead.stage = "sem_interesse"
            db.commit()
            resp = _gerar_resposta_ia(
                estado["historico"],
                "Cliente não tem interesse. Agradeça por ter respondido. "
                "Diga que fica à disposição caso mude de ideia. 1 frase só.",
                lead=lead
            )
            _responder(numero, resp, lead.id, estado, db)
            estado["etapa"] = "concluido"
            _save_estado(lead, estado, db)
            return

        if quer_agendar_explicito:
            estado["etapa"] = "aguardando_tipo"
            _enviar_selecao_tipo(numero, lead.id, estado, db,
                msg_intro="Que bom! 😊 Vou te conectar com um especialista.")
            _save_estado(lead, estado, db)
            return

        # ✅ CORREÇÃO: Tudo que NÃO é agendamento explícito ou recusa
        # vai pra IA responder naturalmente.
        # "sim", "explica", "queria entender", "me conta" — tudo vai pra IA.
        # A IA EXPLICA primeiro. Só sugere reunião depois de muitas trocas.
        user_msgs_count = len([m for m in estado["historico"] if m["role"] == "user"])

        if user_msgs_count <= 3:
            # Primeiras trocas: EXPLIQUE, não empurre reunião
            ctx = (
                "CONTEXTO: Você tentou ligar e o cliente não atendeu. Ele respondeu no WhatsApp. "
                "NÃO se apresente novamente — já fez isso. "
                "O cliente quer ENTENDER como a FLC Bank pode ajudar. EXPLIQUE. "
                "Fale sobre crédito pra negativado, reestruturação, o que for relevante. "
                "Use seu conhecimento sobre produtos pra responder. "
                "NÃO mencione reunião, agendamento ou especialista ainda. "
                "Tire a dúvida dele primeiro. Seja natural. Máximo 2 frases."
            )
        else:
            # Depois de 3 trocas: pode sugerir reunião suavemente
            ctx = (
                "CONTEXTO: Você já explicou sobre a FLC Bank. O cliente está engajado. "
                "Continue respondendo dúvidas com naturalidade. "
                "Se ele parecer interessado, sugira: 'Quer que eu marque uma conversa rápida "
                "com o especialista? Ele detalha tudo pro seu caso.' "
                "Mas só sugira SE o cliente demonstrar interesse. Se ele só tá perguntando, responda. "
                "Máximo 2 frases."
            )

        resp = _gerar_resposta_ia(estado["historico"], ctx, lead=lead)
        _responder(numero, resp, lead.id, estado, db)

        if user_msgs_count >= 5:
            estado["etapa"] = "conversa"
            _atualizar_lead_ia(lead, estado["historico"], db)

        _save_estado(lead, estado, db)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # ✅ FUNÇÃO DE ESCAPE DO AGENDAMENTO
    # Se o cliente muda de assunto, pergunta algo, ou diz que não quer,
    # SAI do fluxo de agendamento e volta pra conversa livre.
    # ─────────────────────────────────────────────────────────────────────────
    if etapa in ("aguardando_tipo", "aguardando_dia", "aguardando_turno", "aguardando_horario"):
        t_esc = texto.lower().strip()
        
        # NÃO escapa se a mensagem contém palavras de agendamento
        eh_sobre_agendamento = any(w in t_esc for w in [
            "vídeo", "video", "ligação", "ligacao", "telefone",
            "whatsapp", "wpp", "zap", "mensagem", "chat",
            "manhã", "manha", "tarde", "horário", "horario",
            "segunda", "terça", "terca", "quarta", "quinta", "sexta",
            "confirmar", "confirmo", "sim", "ok",
        ])
        
        if not eh_sobre_agendamento:
            # Palavras que CLARAMENTE indicam que quer sair do agendamento
            quer_sair = any(frase in t_esc for frase in [
                "outro assunto", "outra coisa", "não quero", "nao quero",
                "ainda não", "ainda nao", "depois", "agora não", "agora nao",
                "cancelar", "cancela", "desistir", "desisto", "parar",
                "não preciso", "nao preciso", "mudei de ideia",
                "sem interesse", "não tenho interesse", "nao tenho interesse",
                "me explica", "como funciona", "qual a taxa", "quais as taxas",
                "quanto custa", "qual o valor", "presencial",
                "pode ser por mensagem", "por aqui mesmo", "por texto",
            ])
            
            # Perguntas gerais (contém "?" e não é sobre horário/dia)
            eh_pergunta = "?" in t_esc and len(t_esc) > 10
            
            if quer_sair or eh_pergunta:
                print(f"🚪 Escape do agendamento: '{texto[:50]}' → volta pra conversa")
                estado["etapa"] = "conversa"
                ctx = (
                    "O cliente estava no processo de agendamento mas mudou de assunto ou fez uma pergunta. "
                    "Responda a pergunta ou dúvida dele com naturalidade. "
                    "NÃO mencione agendamento, reunião ou especialista agora. "
                    "Quando ele demonstrar interesse novamente, você pode sugerir suavemente. "
                    "Máximo 2 frases."
                )
                resp = _gerar_resposta_ia(estado["historico"], ctx, lead=lead)
                _responder(numero, resp, lead.id, estado, db)
                _save_estado(lead, estado, db)
                return

    # ─────────────────────────────────────────────────────────────────────────
    # AGUARDANDO TIPO
    # ─────────────────────────────────────────────────────────────────────────
    if etapa == "aguardando_tipo":
        t = texto.lower().strip()
        
        is_video = t in ("1", "tipo_video") or any(w in t for w in ["video", "vídeo", "chamada", "online"])
        is_ligacao = t in ("2", "tipo_ligacao") or any(w in t for w in ["ligação", "ligacao", "ligar", "telefone", "fone"])
        is_whatsapp = t in ("3", "tipo_whatsapp") or any(w in t for w in ["whatsapp", "wpp", "zap", "mensagem", "texto", "chat"])
        
        if is_video:
            estado["quer_meet"] = True
            estado["tipo_reuniao"] = "video_chamada"
            estado["etapa"] = "aguardando_dia"
            _enviar_selecao_dia(numero, lead.id, estado, db,
                msg_intro="Perfeito, será por vídeo! 🎥\n\nEscolha o melhor dia:")
        elif is_ligacao:
            estado["quer_meet"] = False
            estado["tipo_reuniao"] = "ligacao"
            estado["etapa"] = "aguardando_dia"
            _enviar_selecao_dia(numero, lead.id, estado, db,
                msg_intro="Perfeito, será por ligação! 📞\n\nEscolha o melhor dia:")
        elif is_whatsapp:
            # WhatsApp → encaminha direto pra especialista (igual transferência)
            from db.database import Especialista, Transferencia

            contexto_analise = (
                f"{lead.resumo or ''} {lead.product or ''} "
                f"{' '.join([h.get('content','') for h in estado.get('historico',[])[-5:]])}"
            ).lower()

            area_detectada = "geral"
            palavras_credito = ["crédito","credito","empréstimo","emprestimo","financiamento","capital de giro","antecipação","antecipacao","recebíveis","recebiveis","taxa","juros","parcela","home equity","imóvel","imovel","garantia"]
            palavras_reestruturacao = ["reestruturação","reestruturacao","dívida","divida","renegociar","renegociação","renegociacao","negativado","nome sujo","serasa","spc","recuperação","recuperacao","falência","falencia","endividado","passivo","credores"]
            score_credito = sum(1 for p in palavras_credito if p in contexto_analise)
            score_reestruturacao = sum(1 for p in palavras_reestruturacao if p in contexto_analise)
            if score_reestruturacao > score_credito and score_reestruturacao > 0:
                area_detectada = "reestruturacao"
            elif score_credito > 0:
                area_detectada = "credito"

            esp = db.query(Especialista).filter(Especialista.ativo == True, Especialista.area == area_detectada).order_by(Especialista.atendimentos_ativos.asc()).first()
            if not esp and area_detectada != "geral":
                esp = db.query(Especialista).filter(Especialista.ativo == True, Especialista.area == "geral").order_by(Especialista.atendimentos_ativos.asc()).first()
            if not esp:
                esp = db.query(Especialista).filter(Especialista.ativo == True).order_by(Especialista.atendimentos_ativos.asc()).first()

            if esp:
                lead.ia_pausada = True
                lead.especialista_id = esp.id
                lead.wpp_etapa = "aguardando_especialista"
                contexto = f"Nome: {lead.name or '—'}\nTelefone: {lead.phone}\nEmpresa: {getattr(lead, 'company', '') or '—'}\nProduto: {lead.product or '—'}\nValor: {lead.desired_value or '—'}\nTemperatura: {lead.temperature or '—'}\nResumo: {lead.resumo or '—'}"
                transf = Transferencia(lead_id=lead.id, especialista_id=esp.id, motivo="Cliente escolheu atendimento por WhatsApp", contexto=contexto)
                db.add(transf)
                esp.atendimentos_ativos = (esp.atendimentos_ativos or 0) + 1
                _responder(numero,
                    "Perfeito, será por WhatsApp! 💬\n\n"
                    "Vou te passar agora para o profissional mais indicado pra te ajudar. "
                    "Ele já tem todo o contexto da nossa conversa, aguarda só um momentinho! 😊",
                    lead.id, estado, db)
                if esp.whatsapp:
                    try:
                        notif = (f"🔔 Novo atendimento via WhatsApp!\n\nCliente: {lead.name or '—'}\nTelefone: {lead.phone}\nProduto: {lead.product or '—'}\nResumo: {lead.resumo or '—'}\n\nAcesse o painel pra responder:\n{os.getenv('WEBHOOK_BASE_URL', '')}/painel-especialista")
                        _enviar(esp.whatsapp, notif)
                    except Exception as e:
                        print(f"⚠️ Erro ao notificar especialista: {e}")
                estado["etapa"] = "concluido"
                print(f"💬 {lead.name} escolheu WhatsApp → transferido para {esp.nome}")
            else:
                _responder(numero,
                    "No momento nossos especialistas estão ocupados. "
                    "Vou anotar aqui e assim que um estiver disponível, ele vai entrar em contato pelo WhatsApp! 😊",
                    lead.id, estado, db)
                lead.ia_pausada = True
                lead.wpp_etapa = "aguardando_especialista"
                estado["etapa"] = "concluido"
                print(f"⚠️ {lead.name} escolheu WhatsApp mas nenhum especialista disponível")
        else:
            quer_explicacao = any(w in t for w in [
                "explicar", "explica", "como funciona", "o que é", "o que vocês",
                "saber mais", "me conta", "dúvida", "duvida", "entender",
                "que banco", "que empresa", "quero saber", "fala mais",
                "qual", "quanto", "taxa", "valor", "produto"
            ])

            if quer_explicacao:
                estado["etapa"] = "conversa"
                ctx = (
                    "O cliente quer entender melhor antes de agendar. "
                    "Responda a dúvida dele com naturalidade usando seu conhecimento. "
                    "Ao final, conduza suavemente para agendar quando ele estiver pronto. "
                    "Máximo 2 frases."
                )
                resp = _gerar_resposta_ia(estado["historico"], ctx, lead=lead)
                _responder(numero, resp, lead.id, estado, db)
            else:
                # ✅ CORREÇÃO: Não fica em loop repetindo menu.
                # Se o cliente disse algo inesperado, volta pra conversa e responde.
                estado["etapa"] = "conversa"
                ctx = (
                    "O cliente respondeu algo que não é vídeo nem ligação. "
                    "Ele pode ter mudado de assunto ou ter uma dúvida. "
                    "Responda de forma natural. "
                    "NÃO repita opções de agendamento. "
                    "Se ele quiser agendar depois, ele vai pedir. "
                    "Máximo 2 frases."
                )
                resp = _gerar_resposta_ia(estado["historico"], ctx, lead=lead)
                _responder(numero, resp, lead.id, estado, db)
        _save_estado(lead, estado, db)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # AGUARDANDO DIA
    # ─────────────────────────────────────────────────────────────────────────
    if etapa == "aguardando_dia":
        t = texto.strip()
        import re as _re

        # "Ver mais datas" — resposta por número ou texto
        datas_disp = estado.get("datas_disponiveis", [])
        num_match = _re.match(r"^(\d+)$", t)
        
        if num_match:
            idx = int(num_match.group(1))
            # Último número = "ver mais datas"
            if datas_disp and idx == len(datas_disp) + 1:
                semana = estado.get("semana_offset", 0) + 1
                _enviar_selecao_dia(numero, lead.id, estado, db, semana_offset=semana,
                    msg_intro="Aqui estão mais datas disponíveis:")
                estado["etapa"] = "aguardando_dia"
                _save_estado(lead, estado, db)
                return
            # Número válido = data selecionada
            elif datas_disp and 1 <= idx <= len(datas_disp):
                dia_ref = datas_disp[idx - 1]["id"] + " 00:00"
            else:
                dia_ref = None
        elif t == "ver_mais_datas" or "mais data" in t.lower():
            semana = estado.get("semana_offset", 0) + 1
            _enviar_selecao_dia(numero, lead.id, estado, db, semana_offset=semana,
                msg_intro="Aqui estão mais datas disponíveis:")
            estado["etapa"] = "aguardando_dia"
            _save_estado(lead, estado, db)
            return
        elif _re.match(r"^\d{4}-\d{2}-\d{2}$", t):
            dia_ref = t + " 00:00"
        else:
            dia_ref = _detectar_dia_sem_hora(texto, agora_ref=agora_br)
        
        if not dia_ref:
            _enviar_selecao_dia(numero, lead.id, estado, db,
                msg_intro="Não consegui entender. 😅 Selecione uma opção:")
            _save_estado(lead, estado, db)
            return

        dt_dia = datetime.strptime(dia_ref, "%Y-%m-%d %H:%M")
        print(f"📅 aguardando_dia: dia_ref={dia_ref} | weekday={dt_dia.weekday()}")
        
        if dt_dia.date() <= agora_br.date():
            dt_dia = _proximo_dia_util(agora_br).replace(hour=0, minute=0, second=0, microsecond=0)
        if dt_dia.weekday() >= 5:
            dt_dia = _proximo_dia_util(dt_dia).replace(hour=0, minute=0, second=0, microsecond=0)

        slots = _todos_slots_do_dia(dt_dia, db)
        dia_str = dt_dia.strftime("%Y-%m-%d")

        if not slots:
            _enviar_selecao_dia(numero, lead.id, estado, db,
                msg_intro=f"Sem vagas em {NOMES_DIA[dt_dia.weekday()]} {dt_dia.strftime('%d/%m')}. 😔\nEscolha outro dia:")
            _save_estado(lead, estado, db)
            return

        estado["slots_disponiveis"] = slots
        estado["dia_referencia"] = dia_str
        estado["etapa"] = "aguardando_turno"

        _enviar_selecao_turno(numero, lead.id, estado, db, dia_str)
        _save_estado(lead, estado, db)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # AGUARDANDO TURNO (manhã ou tarde)
    # ─────────────────────────────────────────────────────────────────────────
    if etapa == "aguardando_turno":
        t = texto.strip().lower()
        dia_ref = estado.get("dia_referencia", "")
        slots_todos = estado.get("slots_disponiveis", [])

        is_manha = t in ("1",) or any(w in t for w in ["manhã", "manha", "manha", "morning"])
        is_tarde = t in ("2",) or any(w in t for w in ["tarde", "afternoon"])

        if is_manha:
            estado["turno"] = "manha"
            slots_filtrados = _filtrar_slots_por_turno(slots_todos, "manha")
        elif is_tarde:
            estado["turno"] = "tarde"
            slots_filtrados = _filtrar_slots_por_turno(slots_todos, "tarde")
        else:
            # Não entendeu — reenvia
            _enviar_selecao_turno(numero, lead.id, estado, db, dia_ref,
                msg_intro="Não entendi. 😅 Prefere manhã ou tarde?")
            _save_estado(lead, estado, db)
            return

        if not slots_filtrados:
            # Sem slots no turno escolhido
            outro_turno = "tarde" if estado["turno"] == "manha" else "manhã"
            _enviar_selecao_turno(numero, lead.id, estado, db, dia_ref,
                msg_intro=f"Sem horários disponíveis nesse turno. 😔\nMas temos vagas à {outro_turno}!")
            _save_estado(lead, estado, db)
            return

        estado["slots_disponiveis"] = slots_filtrados
        estado["etapa"] = "aguardando_horario"
        _enviar_selecao_horario(numero, lead.id, estado, db, dia_ref, slots_filtrados)
        _save_estado(lead, estado, db)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # AGUARDANDO HORÁRIO
    # ─────────────────────────────────────────────────────────────────────────
    if etapa == "aguardando_horario":
        slots   = estado.get("slots_disponiveis", [])
        dia_ref = estado.get("dia_referencia", "")
        data_hora = None
        t = texto.strip()

        # ✅ Resposta numérica: "1", "2", "3" etc
        import re as _re
        num_match = _re.match(r"^(\d+)$", t)
        if num_match and slots:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(slots):
                data_hora = slots[idx]["valor"]
                print(f"📅 Horário via menu numérico: {data_hora}")

        # Resposta com "slot_" prefix (legacy)
        if not data_hora and t.startswith("slot_"):
            data_hora = t[5:]

        # Fallback: tenta interpretar texto livre
        if not data_hora:
            data_hora = _gpt_interpretar_horario(texto)

        if not data_hora and dia_ref:
            for pat in [
                r"(\d{1,2})h(\d{2})", r"(\d{1,2}):(\d{2})",
                r"(\d{1,2})\s*h(?:oras?)?",
                r"[àa]s\s*(\d{1,2})(?:\D|$)"
            ]:
                m = _re.search(pat, texto.lower())
                if m:
                    h  = int(m.group(1))
                    mi = int(m.group(2)) if len(m.groups()) > 1 and m.group(2) else 0
                    if 0 <= h <= 23:
                        data_hora = dia_ref + " " + str(h).zfill(2) + ":" + str(mi).zfill(2)
                        break

        if not data_hora:
            if dia_ref and slots:
                _enviar_selecao_horario(numero, lead.id, estado, db, dia_ref, slots,
                    msg_intro="Não entendi. 😅 Responda com o número da opção:")
            else:
                _responder(numero, "Não consegui entender. 😅 Qual horário você prefere?",
                    lead.id, estado, db)
            _save_estado(lead, estado, db)
            return

        norm = _normalizar_slot(datetime.strptime(data_hora, "%Y-%m-%d %H:%M"))

        from datetime import timedelta as _td
        while norm.weekday() >= 5:
            norm += _td(days=1)

        data_hora = norm.strftime("%Y-%m-%d %H:%M")
        print(f"📅 Horário normalizado: {data_hora} (weekday={norm.weekday()})")

        if _horario_ocupado(data_hora, db):
            novos = _todos_slots_do_dia(norm, db)
            if novos:
                estado["slots_disponiveis"] = novos
                _enviar_selecao_horario(numero, lead.id, estado, db, dia_ref, novos,
                    msg_intro="Esse horário já está ocupado. Escolha outro:")
            else:
                estado["etapa"] = "aguardando_dia"
                _enviar_selecao_dia(numero, lead.id, estado, db,
                    msg_intro="Sem vagas nesse dia. 😔 Escolha outro:")
            _save_estado(lead, estado, db)
            return

        estado["data_hora_pendente"] = data_hora
        estado["etapa"] = "confirmar"
        _enviar_confirmacao(numero, lead.id, estado, db, data_hora)
        _save_estado(lead, estado, db)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # CONFIRMAR
    # ─────────────────────────────────────────────────────────────────────────
    if etapa == "confirmar":
        t = texto.lower().strip()
        
        confirmou = t in ("1", "confirmar_sim") or any(s in t for s in [
            "sim", "s", "ok", "pode", "confirmo", "isso", "certo",
            "bora", "yes", "quero", "ótimo", "otimo", "perfeito", "claro", "com certeza"
        ])
        recusou = t in ("2", "confirmar_outro") or any(s in t for s in [
            "não", "nao", "cancela", "outro", "mudar", "errado", "errei", "negativo"
        ])

        if confirmou:
            data_hora = estado.get("data_hora_pendente", "")
            quer_meet = estado.get("quer_meet", True)
            tipo_reuniao = estado.get("tipo_reuniao") or ("video_chamada" if quer_meet else "ligacao")

            if not data_hora:
                estado["etapa"] = "aguardando_dia"
                _responder(numero, "Desculpe, perdi o contexto do horário. 😅 Qual dia você prefere?",
                    lead.id, estado, db)
                _save_estado(lead, estado, db)
                return

            # ✅ CORREÇÃO: Valida fim de semana antes de confirmar
            from datetime import timedelta
            try:
                dt_confirmado = datetime.strptime(data_hora, "%Y-%m-%d %H:%M")
                print(f"📅 confirmar: data_hora={data_hora} | weekday={dt_confirmado.weekday()}")
                if dt_confirmado.weekday() >= 5:
                    # Avança para segunda-feira
                    while dt_confirmado.weekday() >= 5:
                        dt_confirmado += timedelta(days=1)
                    data_hora = dt_confirmado.strftime("%Y-%m-%d %H:%M")
                    data_br = _formatar_data_br(data_hora)
                    estado["data_hora_pendente"] = data_hora
                    _responder(numero,
                        f"Não atendemos aos finais de semana. 😊 O próximo dia útil seria {data_br}. Confirma?",
                        lead.id, estado, db)
                    _save_estado(lead, estado, db)
                    return
            except:
                pass

            if _horario_ocupado(data_hora, db):
                estado["etapa"] = "aguardando_dia"
                resp = _gerar_resposta_ia(estado["historico"], "Horário ocupado. Peça outro dia.", lead=lead)
                _responder(numero, resp, lead.id, estado, db)
                _save_estado(lead, estado, db)
                return

            # Cria sala Daily.co para vídeo chamada (não para ligação ou WhatsApp)
            sala = {"sucesso": False}
            print(f"🔑 tipo_reuniao={tipo_reuniao} | data_hora={data_hora} | lead={lead.name}")
            if tipo_reuniao == "video_chamada":
                try:
                    print(f"🎥 Criando sala Daily.co para {lead.name}...")
                    sala = criar_sala(lead.name or "cliente", data_hora)
                    print(f"🎥 Resultado Daily.co: sucesso={sala.get('sucesso')} | link={sala.get('link_cliente', 'VAZIO')}")
                    if not sala.get("sucesso"):
                        print(f"⚠️ Daily.co NÃO criou sala: {sala}")
                except Exception as e:
                    print(f"❌ EXCEÇÃO ao criar sala Daily.co: {e}")
                    import traceback; traceback.print_exc()

            meeting = Meeting(
                lead_id=lead.id,
                scheduled_at=data_hora,
                tipo=tipo_reuniao,
                status="agendado",
                room_name=sala.get("room_name", "") if sala.get("sucesso") else "",
                room_url=sala.get("room_url", "") if sala.get("sucesso") else "",
                link_cliente=sala.get("link_cliente", "") if sala.get("sucesso") else "",
                link_especialista=sala.get("link_especialista", "") if sala.get("sucesso") else "",
                token_host=sala.get("token_host", "") if sala.get("sucesso") else "",
                token_guest=sala.get("token_guest", "") if sala.get("sucesso") else "",
            )
            db.add(meeting)
            lead.stage = "agendado"
            lead.scheduled_at = data_hora
            db.commit()

            data_br = _formatar_data_br(data_hora)
            link = sala.get("link_cliente", "") if sala.get("sucesso") else ""
            print(f"📋 Confirmando: data_br={data_br} | link={'SIM: '+link if link else 'NÃO GERADO'} | tipo={tipo_reuniao}")

            # ✅ Mensagem de confirmação adaptada ao tipo
            if tipo_reuniao == "video_chamada" and link:
                _responder(numero,
                    "✅ Reunião confirmada para " + data_br + "!\n\n"
                    "🎥 Acesse a sala no horário combinado:\n" + link + "\n\n"
                    "O especialista estará pronto para te atender. Até lá! 😊",
                    lead.id, estado, db)
            elif tipo_reuniao == "video_chamada" and not link:
                _responder(numero,
                    "✅ Reunião por vídeo confirmada para " + data_br + "!\n\n"
                    "Você receberá o link da sala em breve. "
                    "O especialista estará pronto para te atender. Até lá! 😊",
                    lead.id, estado, db)
                print(f"⚠️ Link da sala NÃO gerado para {lead.name} — Daily.co falhou")
            elif tipo_reuniao == "whatsapp":
                _responder(numero,
                    "✅ Atendimento por WhatsApp confirmado para " + data_br + "!\n\n"
                    "💬 No dia e horário combinado, um especialista da FLC Bank "
                    "entrará em contato com você aqui pelo WhatsApp.\n\n"
                    "Fique de olho nas mensagens. Até lá! 😊",
                    lead.id, estado, db)
            else:
                _responder(numero,
                    "✅ Ligação confirmada para " + data_br + "!\n\n"
                    "O especialista vai te ligar no horário combinado. Até lá! 😊",
                    lead.id, estado, db)

            estado["etapa"] = "concluido"
            estado["data_hora_pendente"] = ""
            estado["dia_referencia"] = ""
            _save_estado(lead, estado, db)
            _atualizar_lead_ia(lead, estado["historico"], db)
            return

        elif recusou:
            estado["etapa"] = "aguardando_dia"
            _enviar_selecao_dia(numero, lead.id, estado, db,
                msg_intro="Sem problema! 😊 Escolha outro dia:")
            _save_estado(lead, estado, db)
            return
        else:
            _enviar_confirmacao(numero, lead.id, estado, db, estado.get("data_hora_pendente", ""))
            _save_estado(lead, estado, db)
            return

    # ─────────────────────────────────────────────────────────────────────────
    # CONVERSA LIVRE — Fluxo 3 externo + agendado + continuidade
    # ─────────────────────────────────────────────────────────────────────────
    t_lower = texto.lower().strip()

    # ─────────────────────────────────────────────────────────────────────────
    # ✅ INTERCEPTOR INSTITUCIONAL — dados da empresa quando cliente pede
    # ─────────────────────────────────────────────────────────────────────────
    palavras_institucional = [
        "cnpj", "confiável", "confiavel", "confiança", "confianca",
        "empresa mesmo", "empresa real", "empresa verdadeira",
        "site da empresa", "site de vocês", "site de voces",
        "tem site", "qual o site", "qual site",
        "posso confiar", "como confiar", "é seguro", "eh seguro",
        "comprovante da empresa", "dados da empresa", "documento da empresa",
        "vocês são empresa", "voces sao empresa",
        "é empresa", "eh empresa", "é golpe", "eh golpe",
        "me manda algo da empresa", "material institucional",
        "cartão cnpj", "cartao cnpj",
    ]
    
    if any(p in t_lower for p in palavras_institucional):
        print(f"🏢 Pedido institucional detectado: '{texto[:60]}'")
        _salvar_msg(lead.id, "user", texto, db)
        estado["historico"].append({"role": "user", "content": texto})
        
        # Envia dados + documentos
        enviar_dados_institucionais(lead.phone or lead.wpp_phone or numero)
        
        # Salva a resposta no histórico
        from integrations.whatsapp import get_resposta_institucional
        resp_inst = get_resposta_institucional()
        _salvar_msg(lead.id, "assistant", resp_inst, db)
        estado["historico"].append({"role": "assistant", "content": resp_inst})
        _save_estado(lead, estado, db)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # ✅ INTERCEPTOR CALLBACK — detecta remarcação de horário
    # ─────────────────────────────────────────────────────────────────────────
    if lead.stage == "callback_agendado":
        from db.database import Callback
        import re
        
        # Detecta horário na mensagem (ex: 17h, 15:30, às 14, 9 horas)
        tem_horario = re.search(r'(\d{1,2})\s*(?::|h|hrs?|horas?)\s*(\d{0,2})', t_lower)
        
        # Palavras que indicam remarcação
        quer_remarcar = tem_horario or any(p in t_lower for p in [
            "muda", "mudar", "mudou", "trocar", "troca",
            "remarcar", "remarca", "reagendar", "reagenda",
            "outro horário", "outro horario", "diferente",
            "não vai dar", "nao vai dar", "não posso", "nao posso",
            "me liga", "liga às", "liga as", "liga pra",
            "pode ser", "prefiro",
            "manhã", "manha", "amanhã", "amanha",
            "mais tarde", "mais cedo",
        ])
        
        quer_cancelar = any(p in t_lower for p in [
            "cancelar", "cancela", "não quero", "nao quero",
            "desistir", "desisto", "não precisa", "nao precisa",
            "esquece", "deixa pra lá", "deixa pra la",
        ])
        
        quer_conversar_wpp = any(p in t_lower for p in [
            "conversar aqui", "por aqui", "pelo whatsapp", "pelo wpp",
            "prefiro aqui", "pode ser aqui", "vamos conversar",
            "por aqui mesmo", "aqui mesmo",
        ])
        
        if quer_cancelar:
            cb = db.query(Callback).filter(
                Callback.lead_id == lead.id, Callback.status == "pendente"
            ).first()
            if cb:
                cb.status = "cancelado"
                db.commit()
            
            _salvar_msg(lead.id, "user", texto, db)
            estado["historico"].append({"role": "user", "content": texto})
            _responder(numero,
                "Sem problema! Cancelei a ligação. 😊\n\n"
                "Se mudar de ideia, é só me chamar por aqui. Tenha um ótimo dia!",
                lead.id, estado, db)
            lead.stage = "sem_interesse"
            _save_estado(lead, estado, db)
            db.commit()
            return
        
        elif quer_conversar_wpp:
            cb = db.query(Callback).filter(
                Callback.lead_id == lead.id, Callback.status == "pendente"
            ).first()
            if cb:
                cb.status = "cancelado"
                db.commit()
            
            _salvar_msg(lead.id, "user", texto, db)
            estado["historico"].append({"role": "user", "content": texto})
            lead.stage = "interessado"
            lead.wpp_etapa = "conversa"
            estado["etapa"] = "conversa"
            _responder(numero,
                "Claro, podemos conversar por aqui mesmo! 💬\n\n"
                "Me conta, qual é a sua maior necessidade hoje?",
                lead.id, estado, db)
            _save_estado(lead, estado, db)
            db.commit()
            return
        
        elif quer_remarcar:
            _salvar_msg(lead.id, "user", texto, db)
            estado["historico"].append({"role": "user", "content": texto})
            
            periodo_amanha = any(p in t_lower for p in ["amanhã", "amanha"])
            
            if tem_horario:
                hora = int(tem_horario.group(1))
                minuto = int(tem_horario.group(2)) if tem_horario.group(2) else 0
                horario_novo = f"{hora:02d}:{minuto:02d}"
                periodo = "amanha" if periodo_amanha else "hoje"
                
                # Cancela callback antigo
                cb = db.query(Callback).filter(
                    Callback.lead_id == lead.id, Callback.status == "pendente"
                ).first()
                if cb:
                    cb.status = "cancelado"
                    db.commit()
                
                # Cria novo callback
                from api.callback_scheduler import agendar_callback
                novo_cb = agendar_callback(lead.id, horario_novo, periodo, f"Remarcado pelo WhatsApp: {texto[:60]}", db)
                
                dia_txt = "amanhã" if periodo == "amanha" else "hoje"
                if novo_cb:
                    _responder(numero,
                        f"Combinado! Alterei pra {dia_txt} às {horario_novo}. ✅\n\n"
                        f"Se precisar mudar de novo, é só me falar! 😊",
                        lead.id, estado, db)
                    print(f"🔄 Callback remarcado: {lead.name} → {dia_txt} {horario_novo}")
                else:
                    _responder(numero,
                        f"Anotei! Vou te ligar {dia_txt} às {horario_novo}. ✅",
                        lead.id, estado, db)
                
                _save_estado(lead, estado, db)
                return
            else:
                # Quer remarcar mas não deu horário — pergunta
                _responder(numero,
                    "Claro, posso remarcar! Que horário fica melhor pra você? 😊",
                    lead.id, estado, db)
                _save_estado(lead, estado, db)
                return

    # ─────────────────────────────────────────────────────────────────────────
    # ✅ INTERCEPTOR TRANSFERÊNCIA — cliente pede pra falar com humano
    # ─────────────────────────────────────────────────────────────────────────
    palavras_transferir = [
        "falar com alguém", "falar com alguem", "falar com uma pessoa",
        "falar com humano", "atendente humano", "pessoa real",
        "quero falar com", "preciso falar com",
        "passar pra alguém", "passar pra alguem",
        "transferir", "atendente", "atendimento humano",
        "falar com especialista", "falar com consultor",
        "não quero falar com robô", "nao quero falar com robo",
        "você é robô", "voce e robo", "é uma ia", "e uma ia",
        "quero suporte", "suporte humano",
    ]
    quer_humano = any(p in t_lower for p in palavras_transferir)

    if quer_humano and not getattr(lead, "ia_pausada", False):
        _salvar_msg(lead.id, "user", texto, db)
        estado["historico"].append({"role": "user", "content": texto})

        # Detecta área com base no contexto da conversa
        from db.database import Especialista, Transferencia

        palavras_credito = [
            "crédito", "credito", "empréstimo", "emprestimo", "financiamento",
            "capital de giro", "antecipação", "antecipacao", "recebíveis",
            "recebiveis", "taxa", "juros", "parcela", "valor", "dinheiro",
            "pegar emprestado", "precisando de grana", "preciso de dinheiro",
            "home equity", "imóvel", "imovel", "garantia",
        ]
        palavras_reestruturacao = [
            "reestruturação", "reestruturacao", "dívida", "divida",
            "renegociar", "renegociação", "renegociacao", "negativado",
            "nome sujo", "serasa", "spc", "recuperação", "recuperacao",
            "falência", "falencia", "recuperação judicial", "endividado",
            "reorganizar", "passivo", "credores",
        ]

        # Analisa texto atual + resumo + produto do lead
        contexto_analise = (
            f"{texto} {lead.resumo or ''} {lead.product or ''} "
            f"{' '.join([h.get('content','') for h in estado.get('historico',[])[-5:]])}"
        ).lower()

        area_detectada = "geral"
        score_credito = sum(1 for p in palavras_credito if p in contexto_analise)
        score_reestruturacao = sum(1 for p in palavras_reestruturacao if p in contexto_analise)

        if score_reestruturacao > score_credito and score_reestruturacao > 0:
            area_detectada = "reestruturacao"
        elif score_credito > 0:
            area_detectada = "credito"

        # Busca especialista da área certa (menos atendimentos)
        esp = (
            db.query(Especialista)
            .filter(Especialista.ativo == True, Especialista.area == area_detectada)
            .order_by(Especialista.atendimentos_ativos.asc())
            .first()
        )

        # Fallback: se não tem da área, tenta "geral"
        if not esp and area_detectada != "geral":
            esp = (
                db.query(Especialista)
                .filter(Especialista.ativo == True, Especialista.area == "geral")
                .order_by(Especialista.atendimentos_ativos.asc())
                .first()
            )

        # Fallback final: qualquer especialista disponível
        if not esp:
            esp = (
                db.query(Especialista)
                .filter(Especialista.ativo == True)
                .order_by(Especialista.atendimentos_ativos.asc())
                .first()
            )

        print(f"🎯 Área detectada: {area_detectada} | Especialista: {esp.nome if esp else 'NENHUM'}")

        if esp:
            # Pausa IA e atribui especialista
            lead.ia_pausada = True
            lead.especialista_id = esp.id
            lead.wpp_etapa = "aguardando_especialista"

            # Monta contexto
            contexto = (
                f"Nome: {lead.name or '—'}\n"
                f"Telefone: {lead.phone}\n"
                f"Empresa: {getattr(lead, 'company', '') or '—'}\n"
                f"Produto: {lead.product or '—'}\n"
                f"Valor: {lead.desired_value or '—'}\n"
                f"Temperatura: {lead.temperature or '—'}\n"
                f"Resumo: {lead.resumo or '—'}\n"
            )

            # Cria transferência
            transf = Transferencia(
                lead_id=lead.id,
                especialista_id=esp.id,
                motivo="Cliente pediu atendimento humano",
                contexto=contexto,
            )
            db.add(transf)
            esp.atendimentos_ativos = (esp.atendimentos_ativos or 0) + 1

            # Avisa cliente com nome do especialista
            titulo_esp = esp.titulo or "Especialista"
            _responder(numero,
                f"Claro! Vou te passar para o profissional mais indicado pra te ajudar. "
                f"Ele já tem todo o contexto da nossa conversa, aguarda só um momentinho! 😊",
                lead.id, estado, db)

            # Notifica especialista no WhatsApp pessoal
            if esp.whatsapp:
                try:
                    notif = (
                        f"🔔 Novo atendimento!\n\n"
                        f"Cliente: {lead.name or '—'}\n"
                        f"Telefone: {lead.phone}\n"
                        f"Produto: {lead.product or '—'}\n"
                        f"Resumo: {lead.resumo or '—'}\n\n"
                        f"Acesse o painel pra responder:\n"
                        f"{os.getenv('WEBHOOK_BASE_URL', '')}/painel-especialista"
                    )
                    _enviar(esp.whatsapp, notif)
                    print(f"📱 Notificação enviada pro especialista {esp.nome} ({esp.whatsapp})")
                except Exception as e:
                    print(f"⚠️ Erro ao notificar especialista: {e}")

            _save_estado(lead, estado, db)
            db.commit()
            print(f"🔄 {lead.name} transferido automaticamente para {esp.nome} ({esp.titulo})")
        else:
            # Nenhum especialista disponível — avisa cliente
            _responder(numero,
                "No momento nossos especialistas estão ocupados. "
                "Vou anotar aqui e assim que um estiver disponível, ele vai entrar em contato! 😊",
                lead.id, estado, db)
            lead.ia_pausada = True
            lead.wpp_etapa = "aguardando_especialista"
            _save_estado(lead, estado, db)
            db.commit()
            print(f"⚠️ {lead.name} pediu humano mas nenhum especialista disponível")
        return

    # ✅ CORREÇÃO: Se o cliente escolhe tipo de reunião na conversa livre
    # (porque a IA perguntou por conta própria), redireciona direto
    escolheu_video = any(w in t_lower for w in ["video", "vídeo", "chamada de vídeo", "videochamada"])
    escolheu_ligacao = any(w in t_lower for w in ["ligação", "ligacao", "ligar", "telefone"])

    if escolheu_video or escolheu_ligacao:
        estado["quer_meet"] = escolheu_video
        estado["etapa"] = "aguardando_dia"
        tipo_texto = "vídeo" if escolheu_video else "ligação"
        emoji = "🎥" if escolheu_video else "📞"
        _enviar_selecao_dia(numero, lead.id, estado, db,
            msg_intro=f"Perfeito, será por {tipo_texto}! {emoji}\n\nEscolha o melhor dia:")
        _save_estado(lead, estado, db)
        return

    # Detecta intenção de agendar — palavras FORTES (funcionam sozinhas)
    quer_agendar = any(w in t_lower for w in [
        "agendar", "reunião", "reuniao", "marcar reunião", "marcar reuniao",
        "quero marcar", "quero agendar", "vamos agendar", "vamos marcar",
        "falar com especialista", "falar com humano", "falar com atendente",
    ])

    # Verifica se a IA perguntou sobre agendar e o cliente confirmou
    ultima_ia = ""
    for m in reversed(estado["historico"]):
        if m["role"] == "assistant":
            ultima_ia = m["content"].lower()
            break

    ia_perguntou_agendar = any(w in ultima_ia for w in [
        "agendar", "marcar", "reunião", "reuniao", "especialista",
        "conversa rápida", "conversa rapida", "quer marcar", "posso marcar",
        "bate-papo", "vídeo ou ligação", "video ou ligacao",
    ])

    # "sim" genérico só vale se a IA REALMENTE perguntou sobre agendar
    # E a mensagem inteira é curta (não é "sim mas quero outro assunto")
    cliente_disse_sim = (
        len(t_lower.strip()) < 20 and
        any(t_lower.strip() == w or t_lower.strip().startswith(w + " ") or t_lower.strip().startswith(w + ",") for w in [
            "sim", "ok", "pode", "claro", "quero", "com certeza",
            "isso", "por favor", "pode ser", "beleza", "fechado", "bora",
        ])
    )
    
    # Verifica se a mensagem contém negação ou mudança de assunto
    tem_negacao = any(frase in t_lower for frase in [
        "não", "nao", "outro assunto", "outra coisa", "ainda não", "ainda nao",
        "depois", "agora não", "agora nao",
    ])
    
    if tem_negacao:
        cliente_disse_sim = False
        # NÃO cancela quer_agendar — "quero agendar, não por telefone" é válido

    if quer_agendar or (ia_perguntou_agendar and cliente_disse_sim):
        estado["etapa"] = "aguardando_tipo"
        _enviar_selecao_tipo(numero, lead.id, estado, db,
            msg_intro="Que bom! 😊 Como prefere a reunião?")
        _save_estado(lead, estado, db)
        return

    # ✅ REMOVIDO: Interceptor de dia/hora era muito agressivo.
    # Mencionava "amanhã" em qualquer contexto e já entrava no agendamento.
    # Agora o agendamento só é ativado por intenção EXPLÍCITA (quer_agendar).

    if stage == "agendado":
        ctx = "CONTEXTO: reunião já agendada. Tire dúvidas com simpatia. NÃO tente vender. Seja breve."
    elif stage == "interessado":
        ctx = (
            f"CONTEXTO: {nome} já conversou na ligação e demonstrou interesse. "
            "NÃO se reapresente. Responda dúvidas sobre produtos com naturalidade. "
            "Se o cliente disser que não tem dúvidas ou que está tudo certo, "
            "sugira a reunião com o especialista: "
            "'Ótimo! Então posso marcar uma conversa rápida com nosso especialista? "
            "É gratuita e leva menos de 5 minutos.' "
            "NUNCA diga 'vou agendar' ou marque dia/hora. Máximo 2 frases."
        )
    elif stage == "nao_atendeu":
        ctx = (
            "CONTEXTO: cliente não atendeu a ligação, está conversando pelo WhatsApp. "
            "NÃO se reapresente. Responda dúvidas sobre produtos. "
            "Explique como a FLC Bank pode ajudar. "
            "Só sugira reunião se o cliente demonstrar interesse claro. "
            "NUNCA diga 'vou agendar' ou marque dia/hora. Máximo 2 frases."
        )
    elif not lead.name:
        ctx = (
            "CONTEXTO: cliente externo, nome desconhecido. "
            "Pergunte o nome. Apresente a FLC Bank brevemente. "
            "Entenda o que ele precisa. Máximo 2 frases."
        )
    else:
        ctx = (
            f"CONTEXTO: cliente externo ({lead.name}). "
            "Entenda a necessidade antes de oferecer produto. "
            "Responda dúvidas sobre crédito para negativados ou reestruturação. "
            "Só sugira reunião quando o cliente demonstrar interesse em avançar. "
            "NUNCA diga 'vou agendar' ou marque dia/hora. Máximo 2 frases."
        )

    resp = _gerar_resposta_ia(estado["historico"], ctx, lead=lead)

    # ✅ INTERCEPTOR: Se a IA LITERALMENTE agendou (inventou data/hora),
    # descarta a resposta e envia uma genérica.
    # NÃO redireciona pro agendamento — só previne a IA de inventar horários.
    resp_lower = resp.lower()
    ia_inventou_horario = any(w in resp_lower for w in [
        "agendei", "confirmado para", "marcado para",
        "sua reunião está", "agendado para",
        "amanhã às", "amanha as", "segunda às", "terça às",
        "às 10h", "às 11h", "às 14h", "às 9h", "às 15h",
    ])

    if ia_inventou_horario:
        print(f"🚫 IA inventou horário: '{resp[:80]}' → descartando")
        resp = _gerar_resposta_ia(estado["historico"],
            "ATENÇÃO: Você acabou de inventar um horário de reunião. Isso é PROIBIDO. "
            "Responda a última mensagem do cliente de forma natural. "
            "NÃO mencione dia, hora, agendamento ou reunião. "
            "Apenas continue a conversa. Máximo 2 frases.",
            lead=lead)
        _save_estado(lead, estado, db)
        return

    _responder(numero, resp, lead.id, estado, db)
    _save_estado(lead, estado, db)

    # ✅ Atualiza CRM a cada 6 mensagens (era 10, agora mais frequente)
    if len(estado["historico"]) % 6 == 0:
        _atualizar_lead_ia(lead, estado["historico"], db)


# ── INBOX API ────────────────────────────────────────────────────────────────

@router.get("/inbox/conversas")
def inbox_conversas(db: Session = Depends(get_db)):
    """Lista todas as conversas que têm mensagens no WhatsApp."""
    from sqlalchemy import case

    # Subquery: última mensagem de cada lead
    ultima_msg = (
        db.query(
            WppMensagem.lead_id,
            func.max(WppMensagem.created_at).label("last_at"),
            func.count(WppMensagem.id).label("total_msgs"),
        )
        .group_by(WppMensagem.lead_id)
        .subquery()
    )

    leads_com_msg = (
        db.query(Lead, ultima_msg.c.last_at, ultima_msg.c.total_msgs)
        .join(ultima_msg, Lead.id == ultima_msg.c.lead_id)
        .order_by(desc(ultima_msg.c.last_at))
        .all()
    )

    resultado = []
    for lead, last_at, total in leads_com_msg:
        # Pega a última mensagem para preview
        last_msg = (
            db.query(WppMensagem)
            .filter(WppMensagem.lead_id == lead.id)
            .order_by(desc(WppMensagem.created_at))
            .first()
        )
        # Conta msgs não lidas (do user, após a última assistant)
        ultima_resp = (
            db.query(func.max(WppMensagem.created_at))
            .filter(WppMensagem.lead_id == lead.id, WppMensagem.role == "assistant")
            .scalar()
        )
        nao_lidas = 0
        if ultima_resp:
            nao_lidas = (
                db.query(func.count(WppMensagem.id))
                .filter(
                    WppMensagem.lead_id == lead.id,
                    WppMensagem.role == "user",
                    WppMensagem.created_at > ultima_resp,
                )
                .scalar()
            ) or 0
        else:
            nao_lidas = (
                db.query(func.count(WppMensagem.id))
                .filter(WppMensagem.lead_id == lead.id, WppMensagem.role == "user")
                .scalar()
            ) or 0

        resultado.append({
            "lead_id": lead.id,
            "name": lead.name or "",
            "phone": lead.phone or "",
            "stage": lead.stage or "",
            "temperature": lead.temperature or "",
            "ia_pausada": bool(getattr(lead, "ia_pausada", False)),
            "especialista_id": lead.especialista_id or "",
            "last_msg": last_msg.content[:80] if last_msg else "",
            "last_msg_role": last_msg.role if last_msg else "",
            "last_at": str(last_at) if last_at else "",
            "total_msgs": total or 0,
            "nao_lidas": nao_lidas,
        })

    return resultado


@router.get("/inbox/mensagens/{lead_id}")
def inbox_mensagens(lead_id: str, db: Session = Depends(get_db)):
    """Retorna todas as mensagens de um lead."""
    lead = db.get(Lead, lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    msgs = (
        db.query(WppMensagem)
        .filter(WppMensagem.lead_id == lead_id)
        .order_by(WppMensagem.created_at)
        .all()
    )

    return {
        "lead": {
            "id": lead.id,
            "name": lead.name or "",
            "phone": lead.phone or "",
            "stage": lead.stage or "",
            "temperature": lead.temperature or "",
            "product": lead.product or "",
            "company": getattr(lead, "company", "") or "",
            "resumo": lead.resumo or "",
            "ia_pausada": bool(getattr(lead, "ia_pausada", False)),
            "especialista_id": lead.especialista_id or "",
        },
        "mensagens": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "status": getattr(m, "status", "") or "",
                "created_at": str(m.created_at),
            }
            for m in msgs
        ],
    }


@router.post("/inbox/enviar")
def inbox_enviar(
    lead_id: str = Body(...),
    texto: str = Body(...),
    pausar_ia: bool = Body(True),
    db: Session = Depends(get_db),
):
    """Envia mensagem manual para um lead via WhatsApp e pausa a IA."""
    lead = db.get(Lead, lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}

    numero = lead.wpp_phone or lead.phone or ""
    if not numero:
        return {"erro": "Lead sem telefone"}

    # Envia via WhatsApp
    wamid = _enviar(numero, texto) or ""

    # Salva no histórico
    msg = WppMensagem(lead_id=lead.id, role="assistant", content=texto, wamid=wamid, status="sent")
    db.add(msg)

    # Pausa IA se solicitado
    if pausar_ia and not lead.ia_pausada:
        lead.ia_pausada = True

    db.commit()
    print(f"💬 Inbox → {lead.name or numero}: {texto[:60]}")
    return {"mensagem": "Enviada!"}


@router.post("/inbox/pausar-ia/{lead_id}")
def inbox_pausar_ia(lead_id: str, db: Session = Depends(get_db)):
    """Toggle pausar/ativar IA para um lead."""
    lead = db.get(Lead, lead_id)
    if not lead:
        return {"erro": "Lead não encontrado"}
    lead.ia_pausada = not lead.ia_pausada
    if not lead.ia_pausada:
        lead.especialista_id = ""
    db.commit()
    status = "pausada" if lead.ia_pausada else "ativada"
    return {"mensagem": f"IA {status} para {lead.name or lead.phone}", "ia_pausada": lead.ia_pausada}


# ── TEMPLATES (Meta Cloud API) ───────────────────────────────────────────────

import time as _time
_templates_cache = {"data": None, "ts": 0}
_TEMPLATES_TTL = 300  # 5 min


@router.get("/templates")
def listar_templates():
    """Lista templates aprovados da WABA. Cache de 5 min."""
    import re
    import requests

    now = _time.time()
    if _templates_cache["data"] and (now - _templates_cache["ts"]) < _TEMPLATES_TTL:
        return _templates_cache["data"]

    waba_id = os.getenv("WA_WABA_ID", "")
    token = os.getenv("WA_ACCESS_TOKEN", "")
    if not waba_id or not token:
        return {"erro": "WA_WABA_ID ou WA_ACCESS_TOKEN não configurado"}

    url = f"https://graph.facebook.com/v20.0/{waba_id}/message_templates"
    params = {
        "fields": "name,status,language,components",
        "status": "APPROVED",
        "limit": 100,
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        res = requests.get(url, params=params, headers=headers, timeout=15)
        if res.status_code != 200:
            return {"erro": f"Meta API erro {res.status_code}: {res.text}"}
        data = res.json().get("data", [])
    except Exception as e:
        return {"erro": f"Falha ao chamar Meta: {e}"}

    # Filtro por phone_number_id — só retorna templates do número deste projeto
    # (na Meta, templates são por WABA, mas a WABA é a mesma para os 2 projetos;
    # diferenciamos por prefixo de nome, se configurado via env)
    tpl_prefix = os.getenv("WA_TPL_PREFIX", "")

    parsed = []
    for tpl in data:
        nome = tpl.get("name", "")
        if tpl_prefix and not nome.startswith(tpl_prefix):
            continue
        components = tpl.get("components", [])
        body_text = ""
        for c in components:
            if c.get("type") == "BODY":
                body_text = c.get("text", "")
                break
        variables = [{"index": int(m.group(1))} for m in re.finditer(r"\{\{(\d+)\}\}", body_text)]
        variables.sort(key=lambda v: v["index"])
        parsed.append({
            "name": nome,
            "language": tpl.get("language", "pt_BR"),
            "status": tpl.get("status", ""),
            "body_text": body_text,
            "variables": variables,
        })

    _templates_cache["data"] = parsed
    _templates_cache["ts"] = now
    return parsed