# brain.py — Classificação pós-ligação com GPT-4o-mini
# sofia_responder foi removido — ElevenLabs cuida disso agora

from openai import OpenAI
import os, json, re

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def classificar_lead(historico: list) -> dict:
    conversa = "\n".join([
        f"{'CLIENTE' if m['role'] == 'user' else 'SOFIA'}: {m['content']}"
        for m in historico
    ])

    resposta = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": f"""Analise essa conversa de vendas de crédito financeiro.
Responda APENAS em JSON válido, sem markdown, sem explicações.

CONVERSA:
{conversa}

Formato exato:
{{
  "temperatura": "hot|warm|cold",
  "produto": "empréstimo pessoal|crédito consignado|financiamento|capital de giro|outro|null",
  "valor_desejado": "valor mencionado em reais ou null",
  "urgencia": "now|month|researching",
  "agendado_hora": "horário ou data mencionada para reunião, ou null",
  "resumo": "uma frase descrevendo perfil e necessidade do lead"
}}

Regras:
- hot = demonstrou interesse claro, quer avançar ou agendou
- warm = ouviu com atenção mas não decidiu
- cold = sem interesse, pediu para não ligar, não falou nada útil
- Se o cliente desviou o assunto para temas n relacionados a crédito classifica como cold
- agendado_hora so deve ser preenchido se o cliente EXPLICITAMENTE confirmou um horário específico para a reunião
- produto: identifica pelo contexto — se falou em dívida/empréstimo=empréstimo pessoal, servidor público=consignado, carro/imóvel=financiamento, empresa=capital de giro"""

        }],
        max_tokens=250,
        temperature=0
    )

    try:
        texto = resposta.choices[0].message.content.strip()
        texto = re.sub(r"json|", "", texto).strip()
        resultado = json.loads(texto)
        print(f"   📊 temperatura: {resultado.get('temperatura')} | produto: {resultado.get('produto')} | valor: {resultado.get('valor_desejado')}")
        return resultado
    except Exception as e:
        print(f"❌ Erro ao classificar: {e}")
        return {"temperatura": "cold", "resumo": "Erro ao classificar"}