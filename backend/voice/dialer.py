import requests, os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()

ELEVENLABS_API_KEY         = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID        = os.getenv("ELEVENLABS_AGENT_ID")
ELEVENLABS_PHONE_NUMBER_ID = os.getenv("ELEVENLABS_PHONE_NUMBER_ID")


def _saudacao_horario() -> str:
    """Retorna Bom dia / Boa tarde / Boa noite conforme horário de Brasília."""
    hora = datetime.now(timezone(timedelta(hours=-3))).hour
    if 5 <= hora < 12:
        return "Bom dia"
    elif 12 <= hora < 18:
        return "Boa tarde"
    else:
        return "Boa noite"


def fazer_ligacao(phone: str, nome: str = "") -> str:
    numero = formatar_telefone(phone)

    # Log das variáveis de ambiente (sem expor a key inteira)
    api_key_preview = (ELEVENLABS_API_KEY or "")[:8] + "..." if ELEVENLABS_API_KEY else "NÃO CONFIGURADA"
    print(f"📞 [Dialer] Número: {numero}")
    print(f"📞 [Dialer] Agent ID: {ELEVENLABS_AGENT_ID}")
    print(f"📞 [Dialer] Phone Number ID: {ELEVENLABS_PHONE_NUMBER_ID}")
    print(f"📞 [Dialer] API Key: {api_key_preview}")

    if not ELEVENLABS_API_KEY:
        raise Exception("ELEVENLABS_API_KEY não configurada")
    if not ELEVENLABS_AGENT_ID:
        raise Exception("ELEVENLABS_AGENT_ID não configurado")
    if not ELEVENLABS_PHONE_NUMBER_ID:
        raise Exception("ELEVENLABS_PHONE_NUMBER_ID não configurado")

    url = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "agent_id": ELEVENLABS_AGENT_ID,
        "agent_phone_number_id": ELEVENLABS_PHONE_NUMBER_ID,
        "to_number": numero,
        "telephony_call_config": {
            "ringing_timeout_secs": 20
        },
        "twilio_params": {
            "machine_detection": "DetectMessageEnd",
            "machine_detection_timeout": "3",
            "machine_detection_speech_threshold": "1800",
            "machine_detection_speech_end_threshold": "800",
            "machine_detection_silence_timeout": "3000",
            "async_amd": "true",
            "async_amd_status_callback": (os.getenv("WEBHOOK_BASE_URL", "") + "/api/calls/amd-status"),
            "async_amd_status_callback_method": "POST"
        },
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "nome_cliente": nome or "cliente",
                "saudacao":     _saudacao_horario()
            }
        }
    }

    print(f"📞 [Dialer] Enviando request para ElevenLabs...")
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    print(f"📞 [Dialer] Status: {response.status_code}")
    print(f"📞 [Dialer] Resposta completa: {response.text}")

    if response.status_code not in (200, 201):
        raise Exception(f"Erro ElevenLabs outbound: {response.status_code} — {response.text}")

    data = response.json()
    conversation_id = data.get("conversation_id") or data.get("call_id") or data.get("id") or ""

    if not conversation_id:
        print(f"⚠️ [Dialer] ATENÇÃO: ElevenLabs retornou 200 mas sem conversation_id!")
        print(f"⚠️ [Dialer] Campos disponíveis: {list(data.keys())}")
        print(f"⚠️ [Dialer] Resposta: {data}")
        # Não levanta erro — a ligação pode ter sido enfileirada
        conversation_id = f"pending_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    print(f"✅ Ligação iniciada — conversation_id: {conversation_id}")
    return conversation_id


def formatar_telefone(phone: str) -> str:
    limpo = "".join(c for c in phone if c.isdigit())
    if not limpo.startswith("55"):
        limpo = "55" + limpo
    return "+" + limpo