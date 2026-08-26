"""
Bot de notícias no Telegram — coleta itens de feeds RSS sobre data
center e envia os novos via API do Telegram.

TELEGRAM_TOKEN e CHAT_ID vêm de variáveis de ambiente (localmente,
defina-as no terminal antes de rodar; no GitHub Actions, vêm dos
Secrets do repositório).
"""

import json
import os
import sys
import time

import feedparser
import requests
from googlenewsdecoder import gnewsdecoder

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Feeds 100% dedicados a data center não precisam de filtro por
# palavra-chave; feeds de tema mais amplo (ex: mercado de energia)
# precisam, senão vem notícia irrelevante junto.
FEEDS = [
    {"url": "https://www.datacenterdynamics.com/en/rss/", "filtrar": False},
    {"url": "https://www.datacenterknowledge.com/rss.xml", "filtrar": False},
    {"url": "https://megawhat.uol.com.br/feed/", "filtrar": True},
    {"url": "https://itforum.com.br/feed/", "filtrar": True},
    {"url": "https://www.mobiletime.com.br/feed/", "filtrar": True},
    {"url": "https://tiinside.com.br/feed/", "filtrar": True},
    {"url": "https://telesintese.com.br/feed/", "filtrar": True},
    {"url": "https://convergenciadigital.com.br/feed/", "filtrar": True},
    # Busca por palavra-chave no Google Notícias (Brasil): cobre
    # qualquer veículo indexado (Poder360, Exame, Forbes, G1, etc.).
    {
        "url": "https://news.google.com/rss/search?q=%22data+center%22+OR+%22data+centers%22+OR+%22centro+de+dados%22&hl=pt-BR&gl=BR&ceid=BR:pt-BR",
        "filtrar": True,
    },
]

KEYWORDS = [
    "data center",
    "data centers",
    "datacenter",
    "centro de dados",
    "centros de dados",
]


def contem_palavra_chave(texto: str) -> bool:
    texto_lower = texto.lower()
    return any(k.lower() in texto_lower for k in KEYWORDS)


def resolver_link_google_news(link: str) -> str:
    if not link.startswith("https://news.google.com/"):
        return link
    try:
        resultado = gnewsdecoder(link, interval=1)
        if resultado.get("status"):
            return resultado["decoded_url"]
    except Exception as erro:
        print(f"Não consegui resolver link do Google Notícias: {erro}")
    return link


def escapar_markdown(texto: str) -> str:
    for caractere in ("_", "*", "`", "["):
        texto = texto.replace(caractere, f"\\{caractere}")
    return texto

SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enviados.json")


def carregar_enviados() -> set:
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def salvar_enviados(enviados: set) -> None:
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(enviados), f, ensure_ascii=False, indent=2)


def enviar_telegram(titulo: str, link: str, fonte: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    texto = f"📰 *{escapar_markdown(titulo)}*\n_{escapar_markdown(fonte)}_\n{link}"
    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    resposta = requests.post(url, data=payload, timeout=15)
    if not resposta.ok:
        print(f"Falha ao enviar: {resposta.status_code} {resposta.text}")
    return resposta.ok


def checar_feeds() -> None:
    enviados = carregar_enviados()
    novos = 0

    for feed_info in FEEDS:
        feed_url = feed_info["url"]
        feed = feedparser.parse(feed_url)
        if feed.bozo:
            print(f"Aviso: não consegui ler corretamente {feed_url}")

        fonte_padrao = feed.feed.get("title", feed_url)

        for entrada in feed.entries:
            link = entrada.get("link", "")
            titulo = entrada.get("title", "")
            resumo = entrada.get("summary", "")

            if not link:
                continue

            link = resolver_link_google_news(link)
            if link in enviados:
                continue

            if feed_info["filtrar"] and not contem_palavra_chave(f"{titulo} {resumo}"):
                continue

            fonte_especifica = entrada.get("source", {}).get("title")
            fonte = fonte_especifica or fonte_padrao
            if fonte_especifica and titulo.endswith(f" - {fonte_especifica}"):
                titulo = titulo[: -len(f" - {fonte_especifica}")]

            if enviar_telegram(titulo, link, fonte):
                enviados.add(link)
                novos += 1
                time.sleep(1)  # evita rate limit da API do Telegram

    salvar_enviados(enviados)
    print(f"{novos} nova(s) notícia(s) enviada(s).")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        sys.exit("Defina as variáveis de ambiente TELEGRAM_TOKEN e CHAT_ID antes de rodar.")
    checar_feeds()
