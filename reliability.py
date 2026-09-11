"""Identidade, política de fontes e gravação atômica compartilhadas."""
import json
import os
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def salvar_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def carregar_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def canonica(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
        host = p.hostname.lower().removeprefix("www.")
        port = p.port
        if port and port not in (80, 443):
            host += f":{port}"
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
        return urlunsplit(("https", host, p.path or "/", urlencode(sorted(query)), ""))
    except (ValueError, TypeError):
        return ""


# O nome apresentado pelo RSS nunca concede confiança ao domínio.
DOMINIOS = {
    1: "valor.globo.com folha.uol.com.br estadao.com.br oglobo.globo.com braziljournal.com exame.com poder360.com.br cnnbrasil.com.br infomoney.com.br ft.com wsj.com nytimes.com washingtonpost.com economist.com reuters.com bloomberg.com bloomberglinea.com politico.com axios.com".split(),
    2: "megawhat.uol.com.br epbr.com.br canalenergia.com.br broadcast.com.br neofeed.com.br pipelinevalor.globo.com eixos.com.br brasilenergia.com.br uol.com.br moneytimes.com.br spglobal.com carbon-pulse.com argusmedia.com icis.com carbonbrief.org ecosystemmarketplace.com utilitydive.com canarymedia.com datacenterdynamics.com datacenterfrontier.com theinformation.com semafor.com cnbc.com datacenterknowledge.com itforum.com.br mobiletime.com.br tiinside.com.br telesintese.com.br convergenciadigital.com.br".split(),
}


def nivel_fonte(item):
    host = urlsplit(canonica(item.get("link", ""))).hostname or ""
    for level, domains in DOMINIOS.items():
        if host in domains:
            return level
    return 99


def identidades(item):
    return {c for url in [item.get("link", ""), *item.get("aliases", [])]
            if (c := canonica(url))}
