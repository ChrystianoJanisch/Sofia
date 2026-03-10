import os, requests, uuid
from datetime import datetime, timedelta, timezone

DAILY_API_KEY  = os.getenv("DAILY_API_KEY", "")
DAILY_BASE_URL = "https://api.daily.co/v1"
BASE_URL       = os.getenv("WEBHOOK_BASE_URL", "https://sofia-ai-production.up.railway.app")

HEADERS = {
    "Authorization": f"Bearer {DAILY_API_KEY}",
    "Content-Type": "application/json"
}


def criar_sala(nome_lead: str, data_hora_str: str) -> dict:
    try:
        slug = _gerar_slug(nome_lead, data_hora_str)
        exp  = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())

        payload = {
            "name": slug,
            "properties": {
                "exp": exp,
                "enable_knocking": True,       # cliente fica na sala de espera até host liberar
                "enable_chat": True,
                "enable_screenshare": True,
                "start_video_off": False,
                "start_audio_off": False,
                "lang": "pt-BR",
                "max_participants": 10,
                "eject_at_room_exp": True,     # expulsa todos quando sala expira
                "eject_after_elapsed": 7200,   # expulsa após 2h de reunião
            }
        }

        res = requests.post(f"{DAILY_BASE_URL}/rooms", json=payload, headers=HEADERS, timeout=15)

        if res.status_code in (200, 201):
            data      = res.json()
            room_url  = data.get("url", "")
            room_name = data.get("name", slug)

            # Gera tokens para host e guest
            token_host  = _gerar_token(room_name, is_owner=True,  exp=exp)
            token_guest = _gerar_token(room_name, is_owner=False, exp=exp)

            link_cliente      = f"{BASE_URL}/reuniao/espera/{room_name}"
            link_especialista = f"{BASE_URL}/reuniao/sala/{room_name}"

            print(f"✅ Sala Daily.co criada: {room_name}")
            return {
                "sucesso":          True,
                "room_name":        room_name,
                "room_url":         room_url,
                "link_cliente":     link_cliente,
                "link_especialista":link_especialista,
                "token_host":       token_host,
                "token_guest":      token_guest,
            }
        else:
            print(f"❌ Daily.co erro {res.status_code}: {res.text}")
            return {"sucesso": False}

    except Exception as e:
        print(f"❌ Erro ao criar sala Daily.co: {e}")
        return {"sucesso": False}


def _gerar_token(room_name: str, is_owner: bool, exp: int) -> str:
    """Gera token JWT do Daily.co para host (owner) ou guest."""
    try:
        payload = {
            "properties": {
                "room_name": room_name,
                "is_owner":  is_owner,
                "exp":       exp,
                "start_video_off": False,
                "start_audio_off": False,
            }
        }
        res = requests.post(f"{DAILY_BASE_URL}/meeting-tokens", json=payload, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            return res.json().get("token", "")
        print(f"⚠️ Erro ao gerar token Daily.co: {res.text}")
        return ""
    except Exception as e:
        print(f"⚠️ Erro ao gerar token: {e}")
        return ""


def obter_gravacoes(room_name: str) -> list:
    try:
        res = requests.get(
            f"{DAILY_BASE_URL}/recordings",
            headers=HEADERS,
            params={"room_name": room_name},
            timeout=15
        )
        if res.status_code == 200:
            return res.json().get("data", [])
        return []
    except Exception as e:
        print(f"❌ Erro ao buscar gravações: {e}")
        return []


def _gerar_slug(nome: str, data_hora_str: str) -> str:
    nome_limpo = "".join(c for c in nome.lower() if c.isalnum() or c == " ").split()[0] if nome else "cliente"
    try:
        dt     = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
        sufixo = dt.strftime("%Y%m%d-%Hh")
    except:
        sufixo = datetime.now().strftime("%Y%m%d-%Hh")
    uid = str(uuid.uuid4())[:6]
    return f"flcbank-{nome_limpo}-{sufixo}-{uid}"