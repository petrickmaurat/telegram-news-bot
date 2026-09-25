"""Barreira temática anterior à preferência de veículo, exclusiva do e-mail."""
import re
import unicodedata
from html import unescape


def texto_limpo(texto):
    texto = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", texto or "", flags=re.I | re.S)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", texto))).strip()


PADROES = {
    "data_center": r"\b(data[ -]?cent(?:er|re)s?|centros? de dados|red ata|redata|hyperscal\w*)\b",
    # Componentes e químicas também são evidência: "Silicon anode startup opens
    # pilot line" não diz "battery".
    "baterias": (r"\b(bateri\w*|batter\w*|bess|armazenamento|energy storage|catl|byd|eve energy|"
                 r"gotion|hithium|solid.state|sodium.ion|anod\w*|catod\w*|cathod\w*|lfp|lmfp|"
                 r"iron.air|ferro.ar|long.duration)\b"),
    # Termos do mercado de carbono que não contêm "carbono": projetos de
    # reflorestamento e REDD vendem créditos (ex.: Mombak e o fundo do BNDES).
    "carbono": (r"\b(carbon\w*|emiss\w*|emission\w*|sbce|ets|cap.and.trade|reflorest\w*|"
                r"reforest\w*|restauracao florestal|redd\w*|descarboniz\w*|decarboni\w*|"
                r"offsets?|artigo 6|article 6)\b"),
}


def verificar_tema(item, topico):
    # Não usar URL, nome do veículo ou justificativa da IA como evidência de tema.
    texto = texto_limpo(item.get("titulo", "") + " " + item.get("resumo", ""))
    normal = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    if not re.search(PADROES[topico], normal):
        return False, "Título/trecho disponível sem referência explícita ao tema; associação genérica com energia não basta."
    if "/newsletters/" in item.get("link", "") and len(texto_limpo(item.get("resumo", "")).split()) < 40:
        return False, "Boletim de múltiplos assuntos sem trecho suficiente para isolar o fato do tema."
    return True, "Referência temática localizada; ainda depende da avaliação editorial da relação direta."
