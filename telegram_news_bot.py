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

import requests

from common import coletar_itens_novos

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enviados.json")


def carregar_enviados() -> set:
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def salvar_enviados(enviados: set) -> None:
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(enviados), f, ensure_ascii=False, indent=2)


def escapar_markdown(texto: str) -> str:
    for caractere in ("_", "*", "`", "["):
        texto = texto.replace(caractere, f"\\{caractere}")
    return texto


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

    for item in coletar_itens_novos(enviados):
        if enviar_telegram(item["titulo"], item["link"], item["fonte"]):
            enviados.add(item["link"])
            novos += 1
            time.sleep(1)  # evita rate limit da API do Telegram

    salvar_enviados(enviados)
    print(f"{novos} nova(s) notícia(s) enviada(s).")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        sys.exit("Defina as variáveis de ambiente TELEGRAM_TOKEN e CHAT_ID antes de rodar.")
    checar_feeds()
