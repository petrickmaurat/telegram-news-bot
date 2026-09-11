"""Publica apenas o estado; utilizado sob o lock compartilhado dos workflows."""
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parent
FILES = ["enviados.json", "digest_enviados.json", "google_cache.json",
         "digest_fila.json", "telegram_incerto.json", "digest_incerto.json", "digest_auditoria.json"]


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check)


def persistir():
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
    arquivos = [name for name in FILES if (ROOT / name).exists()]
    if arquivos:
        git("add", "--", *arquivos)
    diff = git("diff", "--cached", "--quiet", check=False)
    if diff.returncode == 1:
        git("commit", "-m", "Atualiza estado do bot", "--", *arquivos)
    elif diff.returncode != 0:
        raise RuntimeError("Não foi possível verificar estado do Git")
    # Mesmo sem novo diff, um commit anterior pode ainda estar sem push.
    for tentativa in range(3):
        git("pull", "--rebase", "origin", os.environ.get("BOT_BRANCH", "main"))
        resultado = git("push", "origin", "HEAD:" + os.environ.get("BOT_BRANCH", "main"), check=False)
        if resultado.returncode == 0:
            return
        time.sleep(2 ** tentativa)
    raise RuntimeError("Estado não persistido no remoto; envio deve permanecer bloqueado")


def checkpoint():
    if os.environ.get("BOT_PERSIST_STATE") == "1":
        persistir()


if __name__ == "__main__":
    persistir()
