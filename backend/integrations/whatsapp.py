import os, requests

EVOLUTION_URL      = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_KEY      = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "IA")


def _formatar_numero(phone: str) -> str:
    digitos = "".join(c for c in phone if c.isdigit())
    if not digitos.startswith("55"):
        digitos = "55" + digitos
    return digitos


def _enviar(numero: str, mensagem: str):
    """Envia mensagem de texto simples via Evolution API."""
    if not EVOLUTION_URL or not EVOLUTION_KEY:
        print(f"⚠️ WhatsApp não configurado — pulando envio para {numero}")
        return

    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_KEY, "Content-Type": "application/json"}
    payload = {"number": numero, "text": mensagem}

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code in (200, 201):
            print(f"✅ WhatsApp enviado para {numero}")
        else:
            print(f"❌ Erro WhatsApp {res.status_code}: {res.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar WhatsApp: {e}")


def enviar_whatsapp(phone: str, nome: str, mensagem: str = None):
    numero = _formatar_numero(phone)
    if mensagem is None:
        mensagem = (
            f"Olá {nome or 'tudo bem'}! 👋\n\n"
            f"Aqui é a Julia da FLC Bank. Tentei te ligar agora mas não consegui falar com você.\n\n"
            f"Temos condições especiais de crédito com acesso a mais de 60 instituições financeiras. "
            f"Quando tiver um momento, me responda aqui e posso te apresentar as opções! 😊"
        )
    _enviar(numero, mensagem)


def enviar_agendamento_whatsapp(phone: str, nome: str, mensagem: str = None):
    numero = _formatar_numero(phone)
    if mensagem is None:
        mensagem = (
            f"Olá {nome or ''}! 😊 Aqui é a Julia da FLC Bank.\n\n"
            f"Foi um prazer falar com você! Para agendarmos sua reunião com um especialista, "
            f"qual dia e horário fica melhor para você?\n\n"
            f"Pode me dizer o dia e a hora que prefere! 📅"
        )
    _enviar(numero, mensagem)


def enviar_confirmacao_agendamento(phone: str, nome: str, horario: str, link_meet: str = None):
    numero = _formatar_numero(phone)
    if link_meet:
        mensagem = (
            f"Olá {nome or ''}! 😊\n\n"
            f"Sua reunião foi agendada para {horario}.\n\n"
            f"🎥 Link da reunião:\n{link_meet}\n\n"
            f"Um especialista da FLC Bank estará te esperando. Qualquer dúvida é só responder aqui!\n\n"
            f"Até logo! 👋"
        )
    else:
        mensagem = (
            f"Olá {nome or ''}! 😊\n\n"
            f"Sua reunião foi agendada para {horario}.\n\n"
            f"📞 Um especialista da FLC Bank vai te ligar no horário combinado. "
            f"Qualquer dúvida é só responder aqui!\n\n"
            f"Até logo! 👋"
        )
    _enviar(numero, mensagem)


# ── ENVIO DE MÍDIA ────────────────────────────────────────────────────────────

def enviar_imagem(phone: str, url_imagem: str, caption: str = ""):
    """Envia imagem via Evolution API (por URL pública)."""
    numero = _formatar_numero(phone)
    if not EVOLUTION_URL or not EVOLUTION_KEY:
        return
    
    endpoint = f"{EVOLUTION_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_KEY, "Content-Type": "application/json"}
    payload = {
        "number": numero,
        "mediatype": "image",
        "media": url_imagem,
        "caption": caption,
    }
    try:
        res = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        if res.status_code in (200, 201):
            print(f"✅ Imagem enviada para {numero}")
        else:
            print(f"❌ Erro imagem {res.status_code}: {res.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar imagem: {e}")


def enviar_documento(phone: str, url_documento: str, filename: str = "documento.pdf", caption: str = ""):
    """Envia documento (PDF, etc) via Evolution API (por URL pública)."""
    numero = _formatar_numero(phone)
    if not EVOLUTION_URL or not EVOLUTION_KEY:
        return
    
    endpoint = f"{EVOLUTION_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_KEY, "Content-Type": "application/json"}
    payload = {
        "number": numero,
        "mediatype": "document",
        "media": url_documento,
        "caption": caption,
        "fileName": filename,
    }
    try:
        res = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        if res.status_code in (200, 201):
            print(f"✅ Documento enviado para {numero}: {filename}")
        else:
            print(f"❌ Erro documento {res.status_code}: {res.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar documento: {e}")


# ── DADOS INSTITUCIONAIS ──────────────────────────────────────────────────────

DADOS_INSTITUCIONAIS = {
    "nome_empresa": os.getenv("EMPRESA_NOME", "FLC Bank - Hub de Crédito"),
    "cnpj": os.getenv("EMPRESA_CNPJ", ""),
    "site": os.getenv("EMPRESA_SITE", ""),
    "instagram": os.getenv("EMPRESA_INSTAGRAM", ""),
    "doc_institucional_url": os.getenv("EMPRESA_DOC_URL", ""),       # URL de PDF institucional
    "doc_institucional_nome": os.getenv("EMPRESA_DOC_NOME", "FLC_Bank_Institucional.pdf"),
    "img_cnpj_url": os.getenv("EMPRESA_IMG_CNPJ_URL", ""),          # URL da imagem do cartão CNPJ
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
            caption="Material Institucional — " + d["nome_empresa"]
        )