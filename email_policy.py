"""Regras exclusivas do digest; não alteram os alertas do Telegram."""
import calendar
import difflib
import math
import re
import time
import unicodedata
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from reliability import canonica, nivel_fonte

IDADE_MAXIMA = 72 * 60 * 60
TOLERANCIA_FUTURO = 15 * 60
RETENCAO_TEXTO = 7 * 24 * 60 * 60
RETENCAO_REGISTRO = 30 * 24 * 60 * 60
LIMIAR_TITULO_SEMELHANTE = 0.65


def data_publicacao(entrada):
    # Não substituir publicação por atualização: uma edição não renova a notícia.
    try:
        if entrada.get("published_parsed"):
            return calendar.timegm(entrada["published_parsed"])
        if entrada.get("published"):
            date = parsedate_to_datetime(entrada["published"])
            if date.tzinfo is not None:
                return date.timestamp()
    except (ValueError, TypeError, OverflowError, IndexError):
        pass
    return None


def recente(item, agora=None):
    date = item.get("publicado_em")
    if type(date) not in (int, float) or not math.isfinite(date):
        return False
    idade = (time.time() if agora is None else agora) - date
    return -TOLERANCIA_FUTURO <= idade <= IDADE_MAXIMA


def google_pendente(item):
    return urlsplit(canonica(item.get("link", ""))).hostname == "news.google.com"


def candidato_admissivel(item):
    # Prioridade de veículo ordena; a lista não é uma barreira de admissão no e-mail.
    return bool(canonica(item.get("link", "")))


def _normalizar_titulo(titulo):
    texto = unicodedata.normalize("NFKD", titulo or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9 ]", " ", texto)


def titulo_semelhante(a, b, limiar=LIMIAR_TITULO_SEMELHANTE):
    """Indica pares para comparação semântica; nunca decide descarte sozinho."""
    if not _normalizar_titulo(a).strip() or not _normalizar_titulo(b).strip():
        return False
    return difflib.SequenceMatcher(None, _normalizar_titulo(a), _normalizar_titulo(b)).ratio() >= limiar


def podar_fila(fila, agora):
    for key, registro in list(fila.items()):
        if registro["status"] == "pendente":
            continue
        encerrado = registro.setdefault("encerrado", agora)
        if agora - encerrado >= RETENCAO_REGISTRO:
            del fila[key]
        elif agora - encerrado >= RETENCAO_TEXTO:
            item = registro["item"]
            registro["item"] = {k: item[k] for k in ("link", "aliases", "topicos", "publicado_em") if k in item}
            registro["compactado"] = True
