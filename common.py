"""
Configuração e coleta de notícias compartilhadas entre o bot do
Telegram (telegram_news_bot.py) e o digest por e-mail (digest_email.py).
"""

import json
import os

import feedparser
from googlenewsdecoder import gnewsdecoder

# Todos os feeds passam pelo filtro de palavra-chave: mesmo os feeds
# "de data center" (DCD, DCK) publicam bastante coisa adjacente (5G,
# satélite, telecom) que não interessa aqui.
FEEDS = [
    {"url": "https://www.datacenterdynamics.com/en/rss/", "filtrar": True},
    {"url": "https://www.datacenterknowledge.com/rss.xml", "filtrar": True},
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


# Cada link do Google Notícias leva ~5s para decodificar, então
# guardamos os já resolvidos em disco para não repetir o trabalho.
_CACHE_GOOGLE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "google_cache.json"
)


def _carregar_cache_google() -> dict:
    if os.path.exists(_CACHE_GOOGLE_FILE):
        with open(_CACHE_GOOGLE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


_cache_google = _carregar_cache_google()


def salvar_cache_google() -> None:
    with open(_CACHE_GOOGLE_FILE, "w", encoding="utf-8") as f:
        json.dump(_cache_google, f, ensure_ascii=False, indent=2)


def resolver_link_google_news(link: str) -> str:
    if not link.startswith("https://news.google.com/"):
        return link
    if link in _cache_google:
        return _cache_google[link]
    try:
        resultado = gnewsdecoder(link, interval=1)
        if resultado.get("status"):
            _cache_google[link] = resultado["decoded_url"]
            return resultado["decoded_url"]
    except Exception as erro:
        print(f"Não consegui resolver link do Google Notícias: {erro}")
    return link


def coletar_itens_novos(ja_vistos: set) -> list:
    """
    Lê todos os feeds e devolve os itens cujo link (já resolvido) não
    está em `ja_vistos`. Não modifica `ja_vistos`. Cada item é um dict
    com titulo, link, fonte e resumo.
    """
    itens = []
    vistos_agora = set()

    for feed_info in FEEDS:
        feed = feedparser.parse(feed_info["url"])
        if feed.bozo:
            print(f"Aviso: não consegui ler corretamente {feed_info['url']}")

        fonte_padrao = feed.feed.get("title", feed_info["url"])

        for entrada in feed.entries:
            link = entrada.get("link", "")
            if not link:
                continue

            link = resolver_link_google_news(link)
            if link in ja_vistos or link in vistos_agora:
                continue

            titulo = entrada.get("title", "")
            resumo = entrada.get("summary", "")

            if feed_info["filtrar"] and not contem_palavra_chave(f"{titulo} {resumo}"):
                continue

            fonte_especifica = entrada.get("source", {}).get("title")
            fonte = fonte_especifica or fonte_padrao
            if fonte_especifica and titulo.endswith(f" - {fonte_especifica}"):
                titulo = titulo[: -len(f" - {fonte_especifica}")]

            vistos_agora.add(link)
            itens.append(
                {"titulo": titulo, "link": link, "fonte": fonte, "resumo": resumo}
            )

    salvar_cache_google()
    return itens
