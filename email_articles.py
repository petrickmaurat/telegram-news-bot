"""Recuperação limitada e reutilizável de dados ausentes no RSS do digest."""
import datetime
import json
import re
from urllib.parse import urlsplit
from html import unescape
from html.parser import HTMLParser

import requests
import trafilatura
from email_policy import recente
from reliability import canonica, identidades

MAX_ARTIGOS = 20
INTERVALO = 6 * 3600


class Metadados(HTMLParser):
    def __init__(self):
        super().__init__()
        self.datas = []
        self.jsonld = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and (attrs.get("property") or attrs.get("name", "")).lower() in (
                "article:published_time", "datepublished", "pubdate", "parsely-pub-date"):
            self.datas.append(attrs.get("content", ""))
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.script = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.jsonld.append("".join(self.script))
            self.script = None


def data_artigo(html):
    parser = Metadados()
    parser.feed(html)

    def visitar(node):
        if isinstance(node, list):
            for child in node:
                visitar(child)
        elif isinstance(node, dict):
            tipos = node.get("@type", [])
            tipos = [tipos] if isinstance(tipos, str) else tipos if isinstance(tipos, list) else []
            if any(t in ("Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting") for t in tipos):
                parser.datas.append(node.get("datePublished", ""))
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visitar(child)
    for script in parser.jsonld:
        try:
            visitar(json.loads(script))
        except (ValueError, TypeError):
            continue
    datas = []
    for valor in parser.datas:
        try:
            date = datetime.datetime.fromisoformat(valor.replace("Z", "+00:00"))
            if date.tzinfo is not None:
                datas.append(date.timestamp())
        except (ValueError, TypeError, AttributeError):
            continue
    # Em caso de conflito, não rejuvenescer uma publicação antiga.
    return min(datas) if datas else None


def texto_curto(item):
    return len(unescape(re.sub(r"<[^>]+>", " ", item.get("resumo", ""))).split()) < 20


def complementar(item, resolver, agora):
    cache = item.get("artigo", {})
    if cache.get("texto") and texto_curto(item):
        item["resumo"] = cache["texto"]
    if item.get("publicado_em") is None and cache.get("publicado_em") is not None:
        item["publicado_em"] = cache["publicado_em"]
    if not texto_curto(item) and item.get("publicado_em") is not None:
        return
    if cache.get("tentado_em", 0) + INTERVALO > agora:
        return
    resultado = {"tentado_em": agora, "resultado": "falha", "texto": cache.get("texto", "")}
    item["artigo"] = resultado
    try:
        link = resolver(item["link"])
        if not canonica(link) or "news.google.com" == urlsplit(link).hostname:
            raise ValueError("Link do artigo não resolvido")
        aliases = identidades(item)
        item["link"] = link
        item["aliases"] = sorted(aliases | identidades(item))
        resposta = requests.get(link, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resposta.raise_for_status()
        html = resposta.text[:2_000_000]
        texto = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
        resultado.update(texto=texto[:3000], publicado_em=data_artigo(html), resultado="consultado")
        if texto_curto(item) and texto:
            item["resumo"] = resultado["texto"]
        if item.get("publicado_em") is None and resultado["publicado_em"] is not None:
            item["publicado_em"] = resultado["publicado_em"]
            item["origem_data"] = "artigo"
        resultado["motivo"] = "Texto e metadados consultados; data de atualização não substitui publicação."
    except Exception as erro:
        resultado["motivo"] = str(erro)


def enriquecer_fila(fila, resolver, agora):
    candidatos = [r["item"] for r in fila.values() if r["status"] == "pendente"
        and (r["item"].get("publicado_em") is None or recente(r["item"], agora))
        and (r["item"].get("publicado_em") is None or texto_curto(r["item"]))]
    candidatos.sort(key=lambda i: i.get("artigo", {}).get("tentado_em", 0))
    tentativas = 0
    for item in candidatos:
        if item.get("artigo", {}).get("tentado_em", 0) + INTERVALO > agora:
            continue
        if tentativas >= MAX_ARTIGOS:
            break
        complementar(item, resolver, agora)
        tentativas += 1
