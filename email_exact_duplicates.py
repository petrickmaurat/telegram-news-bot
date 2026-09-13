"""Unifica cópias da mesma matéria, nunca apenas manchetes parecidas."""
import re
from html import unescape
from urllib.parse import urlsplit
from reliability import canonica, identidades


def normalizar(texto):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", texto or "")).casefold().split())


def unificar(registros, resolver):
    por_titulo = {}
    for r in registros:
        por_titulo.setdefault(normalizar(r["item"]["titulo"]), []).append(r)
    removidos = set()
    duplicados = []
    for titulo, grupo in por_titulo.items():
        if not titulo or len(grupo) < 2:
            continue
        por_url = {}
        for r in grupo:
            item = r["item"]
            if urlsplit(item["link"]).hostname == "news.google.com":
                try:
                    destino = resolver(item["link"])
                    if canonica(destino) and urlsplit(destino).hostname != "news.google.com":
                        aliases = identidades(item)
                        item["link"] = destino
                        item["aliases"] = sorted(aliases | identidades(item))
                except Exception:
                    pass  # Falha de resolução nunca elimina um candidato.
            url = canonica(item["link"])
            if not url or urlsplit(url).hostname == "news.google.com":
                continue
            por_url.setdefault(url, []).append(r)
        for copias in por_url.values():
            copias.sort(key=lambda r: -len(normalizar(r["item"].get("resumo", ""))))
            preservados = []
            for r in copias:
                texto = normalizar(r["item"].get("resumo", ""))
                principal = next((p for p in preservados
                    if texto and texto in normalizar(p["item"].get("resumo", ""))
                    and r["item"].get("publicado_em") == p["item"].get("publicado_em")), None)
                if principal is None:
                    preservados.append(r)
                    continue
                p = principal["item"]
                p["aliases"] = sorted(identidades(p) | identidades(r["item"]))
                p["feeds_origem"] = sorted(set(p.get("feeds_origem", [])) | set(r["item"].get("feeds_origem", [])))
                removidos.add(id(r))
                duplicados.append((r, principal))
    return [r for r in registros if id(r) not in removidos], duplicados
