"""
Bot de notícias no Telegram — lê os feeds RSS (ver common.py) e
envia as notícias novas sobre data center via API do Telegram.

TELEGRAM_TOKEN e CHAT_ID vêm de variáveis de ambiente (localmente,
defina-as no terminal antes de rodar; no GitHub Actions, vêm dos
Secrets do repositório).
"""

import json
import os
import sys
import time
from html import escape

import requests

from common import coletar_itens_novos
from reliability import salvar_json, carregar_json, identidades
from persist_state import checkpoint

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enviados.json")
INCERTO_FILE = os.path.join(os.path.dirname(SENT_FILE), "telegram_incerto.json")


def carregar_enviados() -> set:
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def salvar_enviados(enviados: set) -> None:
    salvar_json(SENT_FILE, sorted(enviados))


def enviar_telegram(titulo: str, link: str, fonte: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    texto = f'📰 <b>{escape(titulo[:500])}</b>\n<i>{escape(fonte[:200])}</i>\n<a href="{escape(link, quote=True)}">Ler matéria</a>'
    payload = {
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for tentativa in range(3):
        # Exceções/5xx permanecem incertos; não repetir um POST possivelmente aceito.
        resposta = requests.post(url, data=payload, timeout=15)
        dados = resposta.json()
        if resposta.status_code == 200 and dados.get("ok") is True:
            return True
        if resposta.status_code == 429:
            atraso = dados.get("parameters", {}).get("retry_after", 1)
            if tentativa < 2 and type(atraso) is int and 0 <= atraso <= 120:
                time.sleep(atraso)
                continue
            return False
        if 400 <= resposta.status_code < 500 and dados.get("ok") is False:
            print(f"Telegram rejeitou mensagem: HTTP {resposta.status_code}")
            return False
        raise RuntimeError("Resposta incerta do Telegram; conferir entrega antes de repetir.")
    return False


def checar_feeds() -> None:
    enviados = carregar_enviados()
    incerto = carregar_json(INCERTO_FILE, None)
    if incerto:
        if set(incerto["aliases"]).issubset(enviados):
            salvar_json(INCERTO_FILE, None)
        else:
            raise RuntimeError("Envio anterior incerto: revisar telegram_incerto.json (README).")
    novos = 0
    falhas = 0
    for item in coletar_itens_novos(enviados, topicos=["data_center"]):
        aliases = identidades(item)
        salvar_json(INCERTO_FILE, {"aliases": sorted(aliases), "item": item})
        checkpoint()  # Registra no remoto ANTES do efeito externo.
        try:
            sucesso = enviar_telegram(item["titulo"], item["link"], item["fonte"])
        except (requests.RequestException, ValueError, RuntimeError):
            # Não incluir a exceção: a URL da API contém o token secreto.
            raise RuntimeError("Entrega Telegram incerta; progresso anterior preservado.") from None
        if sucesso:
            enviados.update(aliases)
            salvar_enviados(enviados)
            novos += 1
            time.sleep(1)
        else:
            falhas += 1
        salvar_json(INCERTO_FILE, None)
        checkpoint()

    salvar_enviados(enviados)
    print(f"{novos} nova(s) notícia(s) enviada(s).")
    if falhas:
        raise RuntimeError(f"{falhas} envio(s) rejeitado(s); mantidos para nova tentativa.")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        sys.exit("Defina as variáveis de ambiente TELEGRAM_TOKEN e CHAT_ID antes de rodar.")
    checar_feeds()
