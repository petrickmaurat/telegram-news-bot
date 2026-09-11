"""
Configuração e coleta de notícias, compartilhadas entre o bot do
Telegram (só data center, tempo real) e o digest por e-mail (data
center + mercado de carbono, curado por IA diariamente).

Cada tópico tem suas próprias palavras-chave e seus próprios feeds.
Feeds marcados com origem "INT" trazem notícia de fora do Brasil.
"""

import json
import os

import feedparser
import requests
from googlenewsdecoder import gnewsdecoder
from reliability import canonica, salvar_json, nivel_fonte
from email_policy import data_publicacao


def _parse_feed(url: str, tentativas: int = 2, timeout: int = 15):
    """feedparser não tem timeout/retry embutido: uma falha de rede
    passageira vira silenciosamente 'feed vazio'. Buscamos via
    requests (com timeout e retry) e só então passamos pro feedparser."""
    for tentativa in range(tentativas):
        try:
            resposta = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
            resposta.raise_for_status()
            return feedparser.parse(resposta.content)
        except Exception as erro:
            if tentativa == tentativas - 1:
                print(f"Aviso: falha ao buscar {url} após {tentativas} tentativa(s): {erro}")
    return feedparser.parse(b"")

# Busca no Google Notícias — cobre qualquer veículo indexado, o que
# nos dá de graça Valor, Folha, Estadão, CNN, Reuters, FT, WaPo, etc.
_GN_DC_BR = (
    "https://news.google.com/rss/search?q=%22data+center%22+OR+%22data+centers%22"
    "+OR+%22centro+de+dados%22&hl=pt-BR&gl=BR&ceid=BR:pt-BR"
)
_GN_DC_US = (
    "https://news.google.com/rss/search?q=%22data+center%22+OR+%22data+centers%22"
    "&hl=en-US&gl=US&ceid=US:en"
)
_GN_CARBONO_BR = (
    "https://news.google.com/rss/search?q=%22mercado+de+carbono%22+OR+%22cr%C3%A9dito"
    "+de+carbono%22+OR+%22cr%C3%A9ditos+de+carbono%22+OR+%22com%C3%A9rcio+de+emiss%C3%B5es%22"
    "+OR+%22SBCE%22&hl=pt-BR&gl=BR&ceid=BR:pt-BR"
)
_GN_CARBONO_US = (
    "https://news.google.com/rss/search?q=%22carbon+market%22+OR+%22cap+and+trade%22"
    "+OR+%22emissions+trading%22&hl=en-US&gl=US&ceid=US:en"
)

TOPICOS = {
    "data_center": {
        "rotulo": "Data Centers",
        "keywords": [
            "data center",
            "data centers",
            "datacenter",
            "centro de dados",
            "centros de dados",
        ],
        "feeds": [
            {"url": "https://www.datacenterdynamics.com/en/rss/", "origem": "INT"},
            {"url": "https://www.datacenterknowledge.com/rss.xml", "origem": "INT"},
            {"url": _GN_DC_BR, "origem": "BR"},
            {"url": _GN_DC_US, "origem": "INT"},
            {"url": "https://megawhat.uol.com.br/feed/", "origem": "BR"},
            {"url": "https://itforum.com.br/feed/", "origem": "BR"},
            {"url": "https://www.mobiletime.com.br/feed/", "origem": "BR"},
            {"url": "https://tiinside.com.br/feed/", "origem": "BR"},
            {"url": "https://telesintese.com.br/feed/", "origem": "BR"},
            {"url": "https://convergenciadigital.com.br/feed/", "origem": "BR"},
        ],
    },
    "carbono": {
        "rotulo": "Mercado de Carbono",
        "keywords": [
            "mercado de carbono",
            "mercado regulado de carbono",
            "crédito de carbono",
            "créditos de carbono",
            "comércio de emissões",
            "precificação de carbono",
            "sbce",
            "carbon market",
            "emissions trading",
            "cap and trade",
        ],
        "feeds": [
            {"url": _GN_CARBONO_BR, "origem": "BR"},
            {"url": _GN_CARBONO_US, "origem": "INT"},
            {"url": "https://megawhat.uol.com.br/feed/", "origem": "BR"},
        ],
    },
}


def contem_palavra_chave(texto: str, keywords: list) -> bool:
    texto_lower = texto.lower()
    return any(k.lower() in texto_lower for k in keywords)


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
    salvar_json(_CACHE_GOOGLE_FILE, _cache_google)


def resolver_link_google_news(link: str) -> str:
    if not canonica(link).startswith("https://news.google.com/"):
        return link
    if link in _cache_google:
        return _cache_google[link]
    try:
        resultado = gnewsdecoder(link, interval=1)
        if resultado.get("status") and canonica(resultado.get("decoded_url", "")):
            _cache_google[link] = resultado["decoded_url"]
            return resultado["decoded_url"]
    except Exception as erro:
        print(f"Não consegui resolver link do Google Notícias: {erro}")
    return link


def coletar_itens_novos(ja_vistos: set, topicos=None, resolver: bool = True) -> list:
    """
    Lê os feeds dos tópicos pedidos e devolve os itens ainda não
    vistos. Não modifica `ja_vistos`.

    topicos: lista de chaves de TOPICOS (default: todos).
    resolver: True mantém a resolução e validação imediatas do Telegram.
        False usa apenas o cache; o digest valida os escolhidos antes do envio.

    Cada item: {titulo, link, fonte, resumo, topico, origem}.
    """
    alvos = topicos or list(TOPICOS)
    itens = []
    vistos_agora = {}
    ja_vistos = {canonica(link) for link in ja_vistos}
    # Migra a comparação dos históricos antigos usando aliases já conhecidos.
    ja_vistos.update(canonica(destino) for original, destino in _cache_google.items()
                     if canonica(original) in ja_vistos)

    for topico in alvos:
        cfg = TOPICOS[topico]
        for feed_info in cfg["feeds"]:
            feed = _parse_feed(feed_info["url"])
            if feed.bozo:
                print(f"Aviso: não consegui ler corretamente {feed_info['url']}")

            fonte_padrao = feed.feed.get("title", feed_info["url"])

            for entrada in feed.entries:
                link = entrada.get("link", "")
                if not link:
                    continue

                titulo = entrada.get("title", "")
                resumo = entrada.get("summary", "")

                if not contem_palavra_chave(f"{titulo} {resumo}", cfg["keywords"]):
                    continue

                original = link
                link = resolver_link_google_news(link) if resolver else _cache_google.get(link, link)
                identidade = canonica(link)
                google_pendente = identidade.startswith("https://news.google.com/")
                if not identidade or (resolver and google_pendente):
                    continue  # resolução indisponível: tentar novamente na próxima coleta
                aliases = {canonica(original), identidade}
                if aliases & ja_vistos:
                    ja_vistos.update(aliases)
                    continue
                if identidade in vistos_agora:
                    existente = vistos_agora[identidade]
                    existente["topicos"] = sorted(set(existente["topicos"]) | {topico})
                    existente["aliases"] = sorted(set(existente["aliases"]) | aliases)
                    if not resolver and existente.get("publicado_em") is None:
                        existente["publicado_em"] = data_publicacao(entrada)
                    continue

                fonte_especifica = (entrada.get("source") or {}).get("title")
                fonte = fonte_especifica or fonte_padrao
                if fonte_especifica and titulo.endswith(f" - {fonte_especifica}"):
                    titulo = titulo[: -len(f" - {fonte_especifica}")]

                item = {
                    "titulo": titulo,
                    "link": link,
                    "fonte": fonte,
                    "resumo": resumo,
                    "topico": topico,
                    "origem": feed_info["origem"],
                    "topicos": [topico],
                    "aliases": sorted(aliases),
                }
                if not resolver:
                    item["publicado_em"] = data_publicacao(entrada)
                if nivel_fonte(item) == 99 and (resolver or not google_pendente):
                    continue
                vistos_agora[identidade] = item
                itens.append(item)

    salvar_cache_google()
    return itens
