"""
Digest por e-mail — roda 2x ao dia. Lê os feeds (ver common.py),
pega o que é novo desde o último digest, usa o Claude Haiku para
filtrar por relevância e resumir cada matéria, monta um e-mail HTML
e envia via API da Brevo.

Variáveis de ambiente necessárias (Secrets do repositório):
    ANTHROPIC_API_KEY  - chave da API da Anthropic (console.anthropic.com)
    BREVO_API_KEY      - chave da API da Brevo (SMTP & API > API Keys)
    EMAIL_REMETENTE    - endereço verificado na Brovo como remetente
    EMAIL_DESTINO      - para quem enviar o digest
"""

import datetime
import json
import os
import re
import sys

import anthropic
import requests
import trafilatura

from common import coletar_itens_novos

MODELO = "claude-haiku-4-5"
MAX_ITENS_PARA_FILTRO = 60
MAX_ITENS_PARA_RESUMO = 25
LIMITE_TEXTO_ARTIGO = 2500  # caracteres enviados ao modelo por matéria

ESTADO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digest_enviados.json")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
EMAIL_REMETENTE = os.environ.get("EMAIL_REMETENTE")
EMAIL_DESTINO = os.environ.get("EMAIL_DESTINO")


def carregar_estado() -> set:
    if os.path.exists(ESTADO_FILE):
        with open(ESTADO_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def salvar_estado(estado: set) -> None:
    with open(ESTADO_FILE, "w", encoding="utf-8") as f:
        json.dump(list(estado), f, ensure_ascii=False, indent=2)


def extrair_json(texto: str):
    """Tenta interpretar a resposta do modelo como JSON, tolerando
    cercas de código ou texto em volta."""
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", texto, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def limpar_html(texto: str) -> str:
    return re.sub(r"<[^>]+>", "", texto or "").strip()


def filtrar_e_ranquear(cliente, itens: list) -> list:
    """Usa o Haiku para escolher os itens relevantes e ordená-los.
    Se algo der errado, devolve todos os itens (limitados)."""
    linhas = [
        f"{i}. [{item['fonte']}] {item['titulo']} — {limpar_html(item['resumo'])[:200]}"
        for i, item in enumerate(itens)
    ]
    prompt = (
        "Você organiza um resumo de notícias sobre DATA CENTERS. Notícias sobre "
        "data centers no BRASIL são prioridade máxima; notícias internacionais só "
        "entram se forem relevantes (grandes investimentos, tecnologia, regulação). "
        "Descarte o que não for sobre data center e itens duplicados.\n\n"
        "Lista de candidatos:\n" + "\n".join(linhas) + "\n\n"
        "Responda APENAS com um array JSON, sem texto antes ou depois, no formato:\n"
        '[{"indice": 0, "prioridade": "alta"}, {"indice": 3, "prioridade": "media"}]\n'
        'prioridade deve ser "alta" (Brasil), "media" ou "baixa" (internacional). '
        "Ordene do mais para o menos importante."
    )
    try:
        resposta = cliente.messages.create(
            model=MODELO,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        dados = extrair_json(resposta.content[0].text)
        if not dados:
            raise ValueError("resposta não-JSON do modelo")
        selecionados = []
        for entrada in dados:
            idx = entrada.get("indice")
            if isinstance(idx, int) and 0 <= idx < len(itens):
                item = dict(itens[idx])
                item["prioridade"] = entrada.get("prioridade", "media")
                selecionados.append(item)
        return selecionados or itens[:MAX_ITENS_PARA_RESUMO]
    except Exception as erro:
        print(f"Filtro por IA falhou ({erro}); mandando todos os itens.")
        return [dict(item, prioridade="media") for item in itens[:MAX_ITENS_PARA_RESUMO]]


def buscar_texto_artigo(link: str) -> str:
    try:
        baixado = trafilatura.fetch_url(link)
        if baixado:
            texto = trafilatura.extract(baixado, include_comments=False, include_tables=False)
            if texto:
                return texto[:LIMITE_TEXTO_ARTIGO]
    except Exception as erro:
        print(f"Não consegui extrair texto de {link}: {erro}")
    return ""


def resumir(cliente, itens: list) -> list:
    """Adiciona a chave 'resumo_final' em cada item. Fallback: usa o
    resumo do próprio feed."""
    for item in itens:
        item["_texto"] = buscar_texto_artigo(item["link"])

    blocos = []
    for i, item in enumerate(itens):
        corpo = item["_texto"] or limpar_html(item["resumo"])
        blocos.append(f"### {i}. {item['titulo']}\n{corpo}")

    prompt = (
        "Para cada matéria abaixo, escreva um resumo objetivo de 2 a 3 frases em "
        "português do Brasil, focando no fato principal e nos números/nomes "
        "relevantes. Não invente informação que não esteja no texto.\n\n"
        + "\n\n".join(blocos)
        + "\n\nResponda APENAS com um array JSON, sem texto antes ou depois:\n"
        '[{"indice": 0, "resumo": "..."}, {"indice": 1, "resumo": "..."}]'
    )
    try:
        resposta = cliente.messages.create(
            model=MODELO,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        dados = extrair_json(resposta.content[0].text) or []
        por_indice = {e["indice"]: e["resumo"] for e in dados if "indice" in e and "resumo" in e}
    except Exception as erro:
        print(f"Resumo por IA falhou ({erro}); usando o texto do feed.")
        por_indice = {}

    for i, item in enumerate(itens):
        item["resumo_final"] = por_indice.get(i) or limpar_html(item["resumo"]) or item["titulo"]
        item.pop("_texto", None)
    return itens


def montar_html(itens: list, momento: str) -> str:
    cor = {"alta": "#c0392b", "media": "#d68910", "baixa": "#7f8c8d"}
    rotulo = {"alta": "Brasil", "media": "Relevante", "baixa": "Internacional"}
    blocos = []
    for item in itens:
        p = item.get("prioridade", "media")
        blocos.append(
            f"""
            <div style="margin:0 0 24px;padding-bottom:20px;border-bottom:1px solid #eee">
              <span style="display:inline-block;font-size:11px;font-weight:700;color:#fff;
                    background:{cor.get(p, '#d68910')};padding:2px 8px;border-radius:3px;
                    text-transform:uppercase">{rotulo.get(p, 'Relevante')}</span>
              <h3 style="margin:8px 0 4px;font-size:17px;line-height:1.35">
                <a href="{item['link']}" style="color:#1a1a1a;text-decoration:none">{item['titulo']}</a>
              </h3>
              <p style="margin:0 0 8px;font-size:12px;color:#888">{item['fonte']}</p>
              <p style="margin:0;font-size:14px;line-height:1.55;color:#333">{item['resumo_final']}</p>
              <p style="margin:6px 0 0;font-size:12px">
                <a href="{item['link']}" style="color:#2980b9">ler matéria &rarr;</a>
              </p>
            </div>"""
        )
    return f"""<!doctype html><html><body style="margin:0;background:#f4f4f4">
      <div style="max-width:640px;margin:0 auto;padding:32px 24px;background:#fff;
            font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
        <h1 style="margin:0 0 4px;font-size:20px">📊 Data centers — {len(itens)} notícia(s)</h1>
        <p style="margin:0 0 28px;font-size:13px;color:#888">{momento}</p>
        {''.join(blocos)}
        <p style="margin:24px 0 0;font-size:11px;color:#aaa">
          Gerado automaticamente a partir de feeds RSS + Google Notícias, filtrado e
          resumido com Claude Haiku.</p>
      </div></body></html>"""


def enviar_email(assunto: str, html: str) -> None:
    resposta = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
            "accept": "application/json",
        },
        json={
            "sender": {"name": "Digest Data Centers", "email": EMAIL_REMETENTE},
            "to": [{"email": EMAIL_DESTINO}],
            "subject": assunto,
            "htmlContent": html,
        },
        timeout=30,
    )
    if not resposta.ok:
        sys.exit(f"Falha ao enviar e-mail: {resposta.status_code} {resposta.text}")
    print("E-mail enviado.")


def rodar_digest() -> None:
    estado = carregar_estado()
    itens = coletar_itens_novos(estado)
    print(f"{len(itens)} item(ns) novo(s) desde o último digest.")

    if not itens:
        print("Nada novo; não vou enviar e-mail.")
        return

    cliente = anthropic.Anthropic()
    selecionados = filtrar_e_ranquear(cliente, itens[:MAX_ITENS_PARA_FILTRO])[:MAX_ITENS_PARA_RESUMO]
    print(f"{len(selecionados)} item(ns) selecionado(s) para o digest.")

    if selecionados:
        selecionados = resumir(cliente, selecionados)
        agora = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
        periodo = "manhã" if agora.hour < 14 else "tarde"
        momento = agora.strftime(f"%d/%m/%Y — {periodo}")
        assunto = f"📊 Data centers — {len(selecionados)} notícia(s) ({agora.strftime('%d/%m')} {periodo})"
        enviar_email(assunto, montar_html(selecionados, momento))

    # Marca como visto tudo que foi coletado (mesmo o que a IA descartou),
    # para não reprocessar no próximo digest.
    for item in itens:
        estado.add(item["link"])
    salvar_estado(estado)


if __name__ == "__main__":
    faltando = [
        nome
        for nome, valor in (
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("BREVO_API_KEY", BREVO_API_KEY),
            ("EMAIL_REMETENTE", EMAIL_REMETENTE),
            ("EMAIL_DESTINO", EMAIL_DESTINO),
        )
        if not valor
    ]
    if faltando:
        sys.exit(f"Faltam variáveis de ambiente: {', '.join(faltando)}")
    rodar_digest()
