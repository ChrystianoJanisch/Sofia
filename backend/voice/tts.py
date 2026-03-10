# tts.py — ElevenLabs com cache de áudio pré-gerado
# Cache elimina o delay de ~4s para frases repetitivas

import requests, os, uuid, hashlib
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID           = os.getenv("ELEVENLABS_VOICE_ID")

# Pasta de cache (áudios pré-gerados ficam aqui entre chamadas)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Frases pré-geradas ao iniciar o servidor (chame gerar_cache_inicial())
FRASES_CACHE = [
    "Olá! Aqui é a Sofia. Tudo bem? Estou ligando porque temos condições especiais de crédito, com acesso a mais de sessenta instituições financeiras. Posso te apresentar as opções?",
    "Entendi! Poderia me dizer qual o valor que você precisa e para qual finalidade?",
    "?",
    "Perfeito! Temos ótimas condições para isso. Você tem alguma restrição no CPF atualmente?",
    "Sem problemas! Trabalhamos com crédito mesmo para negativados. Qual seria o melhor horário para um especialista entrar em contato com você?",
    "Maravilha! Vou registrar seu interesse e em breve um especialista da nossa equipe vai entrar em contato. Obrigada e tenha um ótimo dia!",
    "Compreendo sua preocupação. Nossa consultoria é totalmente gratuita e sem compromisso. O que você acha de só ouvir as opções disponíveis?",
    "Tudo bem, sem problema nenhum! Se mudar de ideia, pode nos ligar. Tenha um ótimo dia!",
    "Um momento, deixa eu verificar as melhores opções para o seu perfil...",
    "Pode deixar que eu anoto. Vou passar para um especialista entrar em contato com você em breve.",
    "Ótimo! Agendado! Você receberá uma confirmação em breve. Obrigada e até logo!",
]

_cache_map: dict[str, str] = {}  # hash → caminho do arquivo


def _hash_texto(texto: str) -> str:
    return hashlib.md5(texto.strip().lower().encode()).hexdigest()


def _chamar_elevenlabs(texto: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    body = {
        "text": texto,
        "model_id": "eleven_flash_v2_5",  # modelo mais rápido da ElevenLabs
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.8,
            "style": 0.2,
            "use_speaker_boost": True,
            "speed": 1.2
        }
    }
    response = requests.post(url, headers=headers, json=body, timeout=15)
    if response.status_code != 200:
        raise Exception(f"Erro ElevenLabs {response.status_code}: {response.text}")
    return response.content


def gerar_cache_inicial():
    """
    Pré-gera áudio para todas as frases comuns.
    Chamado no startup do servidor — elimina delay na primeira ocorrência.
    """
    print("🎙️ Pré-gerando cache de áudio...")
    for frase in FRASES_CACHE:
        h = _hash_texto(frase)
        caminho = os.path.join(CACHE_DIR, f"{h}.mp3")
        if os.path.exists(caminho):
            _cache_map[h] = caminho
            print(f"   ✅ Cache hit: {frase[:50]}...")
            continue
        try:
            audio = _chamar_elevenlabs(frase)
            with open(caminho, "wb") as f:
                f.write(audio)
            _cache_map[h] = caminho
            print(f"   🎵 Gerado: {frase[:50]}...")
        except Exception as e:
            print(f"   ❌ Erro ao pré-gerar: {e}")
    print(f"✅ Cache pronto — {len(_cache_map)} frases em memória")


def gerar_audio_url(texto: str, webhook_base_url: str) -> str:
    """
    Retorna URL do áudio MP3.
    - Se o texto estiver em cache → retorna instantaneamente (~0ms)
    - Se não estiver → chama ElevenLabs e salva em cache para próxima vez
    """
    h = _hash_texto(texto)

    # Verifica cache em memória
    if h in _cache_map and os.path.exists(_cache_map[h]):
        nome_arquivo = os.path.basename(_cache_map[h])
        url = f"{webhook_base_url}/static/cache/{nome_arquivo}"
        print(f"⚡ Cache hit — áudio em 0ms: {texto[:50]}...")
        return url

    # Verifica cache em disco
    caminho_cache = os.path.join(CACHE_DIR, f"{h}.mp3")
    if os.path.exists(caminho_cache):
        _cache_map[h] = caminho_cache
        url = f"{webhook_base_url}/static/cache/{h}.mp3"
        print(f"💾 Cache disco: {texto[:50]}...")
        return url

    # Gera novo áudio via ElevenLabs
    print(f"🎙️ ElevenLabs — gerando áudio para: {texto[:50]}...")
    try:
        audio_bytes = _chamar_elevenlabs(texto)

        # Salva no cache para próxima vez
        with open(caminho_cache, "wb") as f:
            f.write(audio_bytes)
        _cache_map[h] = caminho_cache

        url = f"{webhook_base_url}/static/cache/{h}.mp3"
        print(f"✅ Áudio gerado e cacheado: {url}")
        return url

    except Exception as e:
        print(f"❌ Erro ElevenLabs: {e}")
        raise