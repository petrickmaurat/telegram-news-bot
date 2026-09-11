"""Reconciliação manual após conferir o Telegram ou os logs da Brevo."""
import argparse
from pathlib import Path
from reliability import carregar_json, salvar_json
from persist_state import checkpoint

ROOT = Path(__file__).resolve().parent


def resolver(canal, entregue):
    marker = ROOT / f"{canal}_incerto.json"
    pending = carregar_json(marker, None)
    if not pending:
        raise RuntimeError("Não há entrega incerta neste canal")
    if entregue:
        history = ROOT / ("enviados.json" if canal == "telegram" else "digest_enviados.json")
        state = set(carregar_json(history, []))
        state.update(pending["aliases"])
        salvar_json(history, sorted(state))
    salvar_json(marker, None)
    checkpoint()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("canal", choices=["telegram", "digest"])
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--entregue", action="store_true")
    choice.add_argument("--nao-entregue", action="store_true")
    args = parser.parse_args()
    resolver(args.canal, args.entregue)
