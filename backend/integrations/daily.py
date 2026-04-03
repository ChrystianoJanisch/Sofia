import os, requests, uuid
from datetime import datetime, timedelta, timezone

DAILY_API_KEY  = os.getenv("DAILY_API_KEY", "")
DAILY_BASE_URL = "https://api.daily.co/v1"
BASE_URL       = os.getenv("WEBHOOK_BASE_URL", "https://reuniao.flcbank.com.br")

# Variáveis de configuração
SALA_PREFIX = os.getenv("SALA_PREFIX", "flcbank")

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
                "enable_knocking": True,
                "enable_chat": True,
                "enable_screenshare": True,
                "start_video_off": False,
                "start_audio_off": False,
                "lang": "pt-BR",
                "max_participants": 10,
                "eject_at_room_exp": True,
                "eject_after_elapsed": 7200,
                # ── GRAVAÇÃO AUTOMÁTICA ──
                "enable_recording": "cloud",
            }
        }

        res = requests.post(f"{DAILY_BASE_URL}/rooms", json=payload, headers=HEADERS, timeout=15)

        if res.status_code in (200, 201):
            data      = res.json()
            room_url  = data.get("url", "")
            room_name = data.get("name", slug)

            token_host  = _gerar_token(room_name, is_owner=True,  exp=exp)
            token_guest = _gerar_token(room_name, is_owner=False, exp=exp)

            link_cliente      = f"{BASE_URL}/reuniao/espera/{room_name}"
            link_especialista = f"{BASE_URL}/reuniao/sala/{room_name}"

            print(f"✅ Sala Daily.co criada: {room_name} (gravação habilitada)")
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


def obter_link_gravacao(recording_id: str) -> str:
    try:
        res = requests.get(
            f"{DAILY_BASE_URL}/recordings/{recording_id}/access-link",
            headers=HEADERS,
            timeout=15
        )
        if res.status_code == 200:
            return res.json().get("download_link", "")
        return ""
    except Exception as e:
        print(f"❌ Erro ao obter link da gravação: {e}")
        return ""


def iniciar_gravacao(room_name: str) -> dict:
    try:
        res = requests.post(
            f"{DAILY_BASE_URL}/rooms/{room_name}/recordings/start",
            headers=HEADERS,
            timeout=10
        )
        if res.status_code in (200, 201):
            print(f"🔴 Gravação iniciada: {room_name}")
            return {"sucesso": True}
        print(f"⚠️ Erro ao iniciar gravação: {res.text}")
        return {"sucesso": False}
    except Exception as e:
        print(f"❌ Erro ao iniciar gravação: {e}")
        return {"sucesso": False}


def parar_gravacao(room_name: str) -> dict:
    try:
        res = requests.post(
            f"{DAILY_BASE_URL}/rooms/{room_name}/recordings/stop",
            headers=HEADERS,
            timeout=10
        )
        if res.status_code in (200, 201):
            print(f"⏹️ Gravação parada: {room_name}")
            return {"sucesso": True}
        return {"sucesso": False}
    except Exception as e:
        print(f"❌ Erro ao parar gravação: {e}")
        return {"sucesso": False}


_webhook_ok = False

def configurar_webhook_gravacao():
    """Configura webhook no Daily.co pra receber eventos de gravação (só 1x)."""
    global _webhook_ok
    if _webhook_ok:
        return

    webhook_url = f"{BASE_URL}/api/daily/webhook"

    try:
        res = requests.get(f"{DAILY_BASE_URL}/webhooks", headers=HEADERS, timeout=10)
        if res.status_code == 200:
            body = res.json()
            hooks = body if isinstance(body, list) else body.get("data", [])
            for h in hooks:
                hook_url = h.get("url", "") if isinstance(h, dict) else ""
                if webhook_url in hook_url:
                    _webhook_ok = True
                    print(f"✅ Webhook Daily.co já configurado: {webhook_url}")
                    return

        payload = {
            "url": webhook_url,
        }
        res = requests.post(f"{DAILY_BASE_URL}/webhooks", json=payload, headers=HEADERS, timeout=10)
        if res.status_code in (200, 201):
            _webhook_ok = True
            print(f"✅ Webhook Daily.co configurado: {webhook_url}")
        else:
            print(f"⚠️ Erro ao configurar webhook Daily.co: {res.text}")
    except Exception as e:
        print(f"⚠️ Erro ao configurar webhook: {e}")


def _gerar_slug(nome: str, data_hora_str: str) -> str:
    nome_limpo = "".join(c for c in nome.lower() if c.isalnum() or c == " ").split()[0] if nome else "cliente"
    try:
        dt     = datetime.strptime(data_hora_str, "%Y-%m-%d %H:%M")
        sufixo = dt.strftime("%Y%m%d-%Hh")
    except:
        sufixo = datetime.now().strftime("%Y%m%d-%Hh")
    uid = str(uuid.uuid4())[:6]
    return f"{SALA_PREFIX}-{nome_limpo}-{sufixo}-{uid}"