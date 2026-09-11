"""Validação local de idioma, independente do ranking por IA."""
import re
from functools import lru_cache
from html import unescape


@lru_cache(maxsize=1)
def detector():
    from lingua import LanguageDetectorBuilder
    return LanguageDetectorBuilder.from_all_languages().build()


def idioma_permitido(item):
    texto = unescape(re.sub(r"<[^>]+>", " ", item.get("titulo", "") + " " + item.get("resumo", "")))
    # Siglas latinas não devem mascarar uma manchete escrita em chinês.
    if len(re.findall(r"[\u3400-\u9fff]", texto)) >= 4:
        return False, "Conteúdo com texto em chinês; baterias aceita português e inglês."
    try:
        from lingua import Language
        valores = detector().compute_language_confidence_values(texto[:4000])
        if not valores or valores[0].value < 0.25 or (len(valores) > 1 and valores[0].value - valores[1].value < 0.15):
            return False, "Idioma inconclusivo no título/trecho; candidato permanece pendente."
        permitido = valores[0].language in (Language.PORTUGUESE, Language.ENGLISH)
        return permitido, "Idioma detectado: " + valores[0].language.name
    except Exception as erro:
        return False, "Validação de idioma indisponível; candidato permanece pendente: " + str(erro)
