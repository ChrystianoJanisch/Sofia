import os

_cache = None

def carregar_conhecimento() -> str:
    """
    Carrega todos os .txt da pasta prompts/ e retorna como texto único.
    Resultado é cacheado em memória — só lê do disco uma vez.
    
    Usado por:
    - routes_whatsapp.py → _gerar_resposta_ia() → system prompt
    - Pode ser copiado pro ElevenLabs como base de conhecimento
    """
    global _cache
    if _cache is not None:
        return _cache

    pasta = os.path.dirname(os.path.abspath(__file__))
    conhecimento = []

    for arquivo in sorted(os.listdir(pasta)):
        if arquivo.endswith(".txt"):
            caminho = os.path.join(pasta, arquivo)
            with open(caminho, "r", encoding="utf-8") as f:
                conteudo = f.read().strip()
                if conteudo:
                    conhecimento.append(conteudo)

    _cache = "\n\n".join(conhecimento)
    print(f"📚 Base de conhecimento carregada: {len(conhecimento)} arquivos, {len(_cache)} chars")
    return _cache


def recarregar():
    """Força recarregar do disco (útil após editar .txt sem reiniciar)."""
    global _cache
    _cache = None
    return carregar_conhecimento()
