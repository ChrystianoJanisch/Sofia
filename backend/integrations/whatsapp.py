import os
import requests

# ── CONFIGURAÇÃO META CLOUD API ─────────────────────────────────────────────
IA_NAME = os.getenv("IA_NAME", "Julia")
EMPRESA_NOME = os.getenv("EMPRESA_NOME", "FLC Bank")

WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")

BASE_URL = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def _formatar_numero(phone: str) -> str:
    digitos = "".join(c for c in phone if c.isdigit())
    if not digitos.startswith("55"):
        digitos = "55" + digitos
    return digitos


def _enviar(numero: str, mensagem: str) -> str:
    """Envia mensagem de texto simples via Meta Cloud API. Retorna wamid.
    Só funciona dentro da janela de 24h (cliente mandou mensagem recentemente).
    Para cold outreach use _enviar_template()."""
    if not WA_PHONE_NUMBER_ID or not WA_ACCESS_TOKEN:
        print(f"⚠️ WhatsApp não configurado — pulando envio para {numero}")
        return ""

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "text",
        "text": {"body": mensagem},
    }

    try:
        res = requests.post(BASE_URL, json=payload, headers=HEADERS, timeout=10)
        if res.status_code in (200, 201):
            data = res.json()
            wamid = ""
            messages = data.get("messages", [])
            if messages:
                wamid = messages[0].get("id", "")
            print(f"✅ WhatsApp enviado para {numero} (wamid: {wamid[:20]})")
            return wamid
        else:
            print(f"❌ Erro WhatsApp {res.status_code}: {res.text}")
            return ""
    except Exception as e:
        print(f"❌ Erro ao enviar WhatsApp: {e}")
        return ""


def _enviar_template(numero: str, template_name: str, params: list, lang: str = "pt_BR") -> str:
    """Envia template aprovado via Meta Cloud API. Retorna wamid.
    Use para cold outreach (mensagens fora da janela de 24h).

    params: lista de strings na ordem das variáveis {{1}}, {{2}}, etc.
    """
    if not WA_PHONE_NUMBER_ID or not WA_ACCESS_TOKEN:
        print(f"⚠️ WhatsApp não configurado — pulando template {template_name} para {numero}")
        return ""

    body_params = [{"type": "text", "text": str(p) if p else " "} for p in params]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": [{"type": "body", "parameters": body_params}] if params else [],
        },
    }

    try:
        res = requests.post(BASE_URL, json=payload, headers=HEADERS, timeout=10)
        if res.status_code in (200, 201):
            data = res.json()
            wamid = ""
            messages = data.get("messages", [])
            if messages:
                wamid = messages[0].get("id", "")
            print(f"✅ Template '{template_name}' enviado para {numero} (wamid: {wamid[:20]})")
            return wamid
        else:
            print(f"❌ Erro template '{template_name}' {res.status_code}: {res.text}")
            return ""
    except Exception as e:
        print(f"❌ Erro ao enviar template '{template_name}': {e}")
        return ""


# ── TEMPLATES (configuráveis por env vars) ──────────────────────────────────
TPL_FOLLOWUP_NAO_ATENDEU = os.getenv("WA_TPL_FOLLOWUP_NAO_ATENDEU", "followup_ligacao_nao_atendido")
TPL_CALLBACK_CONFIRMADO  = os.getenv("WA_TPL_CALLBACK_CONFIRMADO", "callback_confirmado")
TPL_LEMBRETE_REUNIAO     = os.getenv("WA_TPL_LEMBRETE_REUNIAO", "lembrete_reuniao")
TPL_CONFIRMACAO_VIDEO    = os.getenv("WA_TPL_CONFIRMACAO_VIDEO", "confirmacao_reuniao_video")
TPL_CONFIRMACAO_LIGACAO  = os.getenv("WA_TPL_CONFIRMACAO_LIGACAO", "confirmacao_reuniao_ligacao")
TPL_CONFIRMACAO_WPP      = os.getenv("WA_TPL_CONFIRMACAO_WPP", "confirmacao_atendimento_wpp")
TPL_FOLLOWUP_SEMANAL     = os.getenv("WA_TPL_FOLLOWUP_SEMANAL", "followup_semanal")
TPL_AGENDAMENTO_POS_LIG  = os.getenv("WA_TPL_AGENDAMENTO_POS_LIG", "agendamento_pos_ligacao")


def enviar_whatsapp(phone: str, nome: str, mensagem: str = None) -> str:
    """Envia follow-up pós-ligação via template aprovado. Retorna wamid."""
    numero = _formatar_numero(phone)
    return _enviar_template(
        numero,
        TPL_FOLLOWUP_NAO_ATENDEU,
        [nome or "tudo bem", IA_NAME, EMPRESA_NOME],
    )


def enviar_agendamento_whatsapp(phone: str, nome: str, mensagem: str = None) -> str:
    """Envia convite de agendamento pós-ligação via template."""
    numero = _formatar_numero(phone)
    return _enviar_template(
        numero,
        TPL_AGENDAMENTO_POS_LIG,
        [nome or "tudo bem", IA_NAME, EMPRESA_NOME],
    )


def enviar_confirmacao_agendamento(phone: str, nome: str, horario: str, link_meet: str = None) -> str:
    """Envia confirmação de reunião via template (vídeo com link ou ligação)."""
    numero = _formatar_numero(phone)
    if link_meet:
        return _enviar_template(numero, TPL_CONFIRMACAO_VIDEO, [horario, link_meet])
    return _enviar_template(numero, TPL_CONFIRMACAO_LIGACAO, [horario])


def enviar_callback_confirmado(phone: str, nome: str, dia: str, hora: str) -> str:
    """Envia confirmação de callback agendado via template."""
    numero = _formatar_numero(phone)
    return _enviar_template(
        numero,
        TPL_CALLBACK_CONFIRMADO,
        [nome or "tudo bem", IA_NAME, EMPRESA_NOME, dia, hora],
    )


def enviar_lembrete_reuniao(phone: str, nome: str, hora: str, tipo: str) -> str:
    """Envia lembrete de reunião via template."""
    numero = _formatar_numero(phone)
    return _enviar_template(
        numero,
        TPL_LEMBRETE_REUNIAO,
        [nome or "Cliente", IA_NAME, EMPRESA_NOME, hora, tipo],
    )


def enviar_confirmacao_wpp(phone: str, horario: str) -> str:
    """Envia confirmação de atendimento por WhatsApp via template."""
    numero = _formatar_numero(phone)
    return _enviar_template(numero, TPL_CONFIRMACAO_WPP, [horario, EMPRESA_NOME])


def enviar_followup_semanal(phone: str, nome: str) -> str:
    """Envia follow-up semanal para lead interessado via template."""
    numero = _formatar_numero(phone)
    return _enviar_template(
        numero,
        TPL_FOLLOWUP_SEMANAL,
        [nome or "tudo bem", IA_NAME, EMPRESA_NOME],
    )


# ── ENVIO DE MÍDIA ───────────────────────────────────────────────────────────

def enviar_imagem(phone: str, url_imagem: str, caption: str = ""):
    """Envia imagem via Meta Cloud API (por URL pública)."""
    numero = _formatar_numero(phone)
    if not WA_PHONE_NUMBER_ID or not WA_ACCESS_TOKEN:
        return

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "image",
        "image": {
            "link": url_imagem,
            "caption": caption,
        },
    }

    try:
        res = requests.post(BASE_URL, json=payload, headers=HEADERS, timeout=15)
        if res.status_code in (200, 201):
            print(f"✅ Imagem enviada para {numero}")
        else:
            print(f"❌ Erro imagem {res.status_code}: {res.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar imagem: {e}")


def enviar_documento(phone: str, url_documento: str, filename: str = "documento.pdf", caption: str = ""):
    """Envia documento (PDF, etc) via Meta Cloud API (por URL pública)."""
    numero = _formatar_numero(phone)
    if not WA_PHONE_NUMBER_ID or not WA_ACCESS_TOKEN:
        return

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "document",
        "document": {
            "link": url_documento,
            "caption": caption,
            "filename": filename,
        },
    }

    try:
        res = requests.post(BASE_URL, json=payload, headers=HEADERS, timeout=15)
        if res.status_code in (200, 201):
            print(f"✅ Documento enviado para {numero}: {filename}")
        else:
            print(f"❌ Erro documento {res.status_code}: {res.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar documento: {e}")


# ── DADOS INSTITUCIONAIS ─────────────────────────────────────────────────────

DADOS_INSTITUCIONAIS = {
    "nome_empresa": os.getenv("EMPRESA_NOME", "FLC Bank - Hub de Crédito"),
    "cnpj": os.getenv("EMPRESA_CNPJ", ""),
    "site": os.getenv("EMPRESA_SITE", ""),
    "instagram": os.getenv("EMPRESA_INSTAGRAM", ""),
    "doc_institucional_url": os.getenv("EMPRESA_DOC_URL", ""),
    "doc_institucional_nome": os.getenv("EMPRESA_DOC_NOME", "FLC_Bank_Institucional.pdf"),
    "img_cnpj_url": os.getenv("EMPRESA_IMG_CNPJ_URL", ""),
}


def get_resposta_institucional() -> str:
    """Monta a resposta com dados institucionais da empresa."""
    d = DADOS_INSTITUCIONAIS
    partes = ["Claro! 😊 Para sua segurança, seguem nossos dados oficiais:\n"]
    partes.append(f"🏢 *{d['nome_empresa']}*")
    if d["cnpj"]:
        partes.append(f"📋 CNPJ: {d['cnpj']}")
    if d["site"]:
        partes.append(f"🌐 Site: {d['site']}")
    if d["instagram"]:
        partes.append(f"📱 Instagram: {d['instagram']}")
    partes.append("\nSe precisar de mais alguma comprovação, é só me pedir! 💼")
    return "\n".join(partes)


def enviar_dados_institucionais(phone: str):
    """Envia texto institucional + documentos opcionais."""
    numero = _formatar_numero(phone)
    d = DADOS_INSTITUCIONAIS

    # 1. Envia texto com dados
    _enviar(numero, get_resposta_institucional())

    # 2. Envia imagem do cartão CNPJ (se configurada)
    if d["img_cnpj_url"]:
        enviar_imagem(numero, d["img_cnpj_url"], caption="Cartão CNPJ — " + d["nome_empresa"])

    # 3. Envia documento institucional (se configurado)
    if d["doc_institucional_url"]:
        enviar_documento(
            numero,
            d["doc_institucional_url"],
            filename=d["doc_institucional_nome"],
            caption="Material Institucional — " + d["nome_empresa"],
        )