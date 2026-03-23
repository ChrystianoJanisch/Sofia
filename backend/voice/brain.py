# brain.py — Classificação pós-ligação com GPT-4o-mini

from openai import OpenAI
import os, json, re

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def classificar_lead(historico: list) -> dict:
    conversa = "\n".join([
        f"{'CLIENTE' if m['role'] == 'user' else 'JULIA'}: {m['content']}"
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
  "produto": "empréstimo pessoal|crédito consignado|financiamento|capital de giro|garantia de imóvel|reestruturação|outro|null",
  "valor_desejado": "valor mencionado em reais ou null",
  "urgencia": "now|month|researching",
  "agendado_hora": "horário ou data mencionada para reunião, ou null",
  "resumo": "uma frase descrevendo perfil e necessidade do lead"
}}

Regras RIGOROSAS de classificação:

HOT (interesse claro):
- Cliente EXPLICITAMENTE disse que quer avançar, agendar, saber mais
- Cliente confirmou horário para reunião
- Cliente pediu pra mandar no WhatsApp pra continuar a conversa
- Cliente fez perguntas detalhadas sobre produto, taxa, prazo

WARM (pode ter interesse, mas não confirmou):
- Cliente ouviu e disse "vou pensar", "depois vejo", "me manda informações"
- Cliente pediu pra ligar em outro horário (callback)
- Cliente não rejeitou mas também não avançou

COLD (sem interesse):
- Cliente disse "não tenho interesse", "não quero", "não preciso"
- Cliente pediu pra não ligar mais
- Cliente desligou ou ficou em silêncio
- Cliente desviou o assunto pra temas não relacionados a crédito
- Cliente foi educado mas NUNCA demonstrou interesse real no produto
- Cliente apenas ouviu a apresentação e disse "ok" ou "tá" sem engajar
- Secretária atendeu e o cliente nunca falou
- Conversa muito curta sem engajamento do cliente

IMPORTANTE:
- Ser educado NÃO é interesse. "Tá", "ok", "entendi" sem perguntas = COLD
- Ouvir a apresentação sem fazer perguntas = COLD
- Só contar como WARM ou HOT se o cliente ATIVAMENTE demonstrou interesse
- agendado_hora só deve ser preenchido se o cliente EXPLICITAMENTE confirmou um horário
- Na dúvida entre warm e cold, classifique como COLD"""

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