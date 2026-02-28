from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import os
from dotenv import load_dotenv

load_dotenv()

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

def fazer_ligacao(phone: str, webhook_base_url: str) -> str:
    call = twilio_client.calls.create(
        to=formatar_telefone(phone),
        from_=TWILIO_NUMBER,
        url=f"{webhook_base_url}/api/calls/atender",
        status_callback=f"{webhook_base_url}/api/calls/status",
        status_callback_method="POST"
    )
    return call.sid

def gerar_abertura(nome: str = "") -> str:
    saudacao = f", {nome}" if nome else ""
    response = VoiceResponse()
    response.say(
        f"Olá{saudacao}! Aqui é a Sofia. Tudo bem? "
        "Estou ligando porque temos condições especiais de crédito, "
        "com acesso a mais de sessenta instituições financeiras. "
        "Posso te apresentar as opções?",
        voice="Polly.Camila-Neural",
        language="pt-BR"
    )
    gather = Gather(
        input="speech",
        language="pt-BR",
        timeout=5,
        action="/api/calls/responder",
        method="POST"
    )
    response.append(gather)
    return str(response)

def formatar_telefone(phone: str) -> str:
    limpo = "".join(c for c in phone if c.isdigit())
    if not limpo.startswith("55"):
        limpo = "55" + limpo
    return "+" + limpo