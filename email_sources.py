"""Catálogo e consultas por veículo, exclusivos do e-mail."""
from copy import deepcopy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from reliability import DOMINIOS, canonica

NOMES = {
    "valor.globo.com": "Valor Econômico", "folha.uol.com.br": "Folha de S.Paulo",
    "estadao.com.br": "Estadão", "oglobo.globo.com": "O Globo", "braziljournal.com": "Brazil Journal",
    "exame.com": "Exame", "poder360.com.br": "Poder360", "cnnbrasil.com.br": "CNN Brasil",
    "infomoney.com.br": "InfoMoney", "ft.com": "Financial Times", "wsj.com": "Wall Street Journal",
    "nytimes.com": "New York Times", "washingtonpost.com": "Washington Post", "economist.com": "The Economist",
    "reuters.com": "Reuters", "bloomberg.com": "Bloomberg", "bloomberglinea.com": "Bloomberg Línea",
    "politico.com": "Politico", "axios.com": "Axios", "megawhat.uol.com.br": "MegaWhat",
    "epbr.com.br": "epbr", "canalenergia.com.br": "CanalEnergia", "broadcast.com.br": "Broadcast",
    "neofeed.com.br": "NeoFeed", "pipelinevalor.globo.com": "Pipeline Valor", "eixos.com.br": "Agência Eixos",
    "brasilenergia.com.br": "Brasil Energia", "uol.com.br": "UOL", "moneytimes.com.br": "Money Times",
    "spglobal.com": "S&P Global", "carbon-pulse.com": "Carbon Pulse", "argusmedia.com": "Argus Media",
    "icis.com": "ICIS", "carbonbrief.org": "Carbon Brief", "ecosystemmarketplace.com": "Ecosystem Marketplace",
    "utilitydive.com": "Utility Dive", "canarymedia.com": "Canary Media", "datacenterdynamics.com": "Data Center Dynamics",
    "datacenterfrontier.com": "Data Center Frontier", "theinformation.com": "The Information", "semafor.com": "Semafor",
    "cnbc.com": "CNBC", "datacenterknowledge.com": "Data Center Knowledge", "itforum.com.br": "IT Forum",
    "mobiletime.com.br": "Mobile Time", "tiinside.com.br": "TI Inside", "telesintese.com.br": "Tele.Síntese",
    "convergenciadigital.com.br": "Convergência Digital",
    "agenciainfra.com": "Agência iNFRA", "gov.br/mme": "MME",
    # Governo e Congresso: publicam regulação (SBCE, Redata, leilões de baterias).
    "gov.br/fazenda": "Ministério da Fazenda", "gov.br/mma": "Ministério do Meio Ambiente",
    "gov.br/mdic": "MDIC", "gov.br/aneel": "Aneel", "epe.gov.br": "EPE",
    "agenciabrasil.ebc.com.br": "Agência Brasil", "agenciagov.ebc.com.br": "Agência Gov",
    "camara.leg.br": "Câmara dos Deputados", "senado.leg.br": "Senado Federal",
    # Carbono: veículos especializados, já que os prioritários publicam pouco do tema.
    "capitalreset.uol.com.br": "Capital Reset", "umsoplaneta.globo.com": "Um Só Planeta",
    "qcintel.com": "Quantum Commodity Intelligence", "carbonherald.com": "Carbon Herald",
    "climatechangenews.com": "Climate Home News",
}
GOVERNO = {"gov.br/mme", "gov.br/fazenda", "gov.br/mma", "gov.br/mdic", "gov.br/aneel",
           "epe.gov.br", "agenciabrasil.ebc.com.br", "agenciagov.ebc.com.br",
           "camara.leg.br", "senado.leg.br"}
CARBONO = {"capitalreset.uol.com.br", "umsoplaneta.globo.com", "qcintel.com", "carbonherald.com",
           "climatechangenews.com"}
FONTES = sorted({*DOMINIOS[1], *DOMINIOS[2], "agenciainfra.com", *GOVERNO, *CARBONO})
# Veículos brasileiros fora de .br. Sem esta lista, a busca por veículo rodava na
# edição americana do Google Notícias, que quase não devolve matérias deles
# (Valor: 1 resultado contra 100 na edição brasileira).
BRASILEIROS_SEM_BR = {"agenciainfra.com", "braziljournal.com", "exame.com"}


def veiculo_brasileiro(site):
    domain = site.partition("/")[0]
    return (domain.endswith(".br") or domain.endswith(".globo.com")
            or domain in BRASILEIROS_SEM_BR)

# Preferência editorial absoluta entre notícias elegíveis, exclusiva do digest.
FONTES_MAXIMAS = {"braziljournal.com", "megawhat.uol.com.br", "valor.globo.com",
                  "pipelinevalor.globo.com", "agenciainfra.com", "eixos.com.br"}


def fonte_maxima(item):
    host = urlsplit(canonica(item.get("link", ""))).hostname or ""
    return any(host == site or host.endswith("." + site) for site in FONTES_MAXIMAS)


def fonte_prioritaria(item):
    p = urlsplit(canonica(item.get("link", "")))
    for site in FONTES:
        domain, _, path = site.partition("/")
        if path:
            # gov.br/fazenda: o caminho identifica o órgão dentro de gov.br.
            if p.hostname == domain and (p.path == "/" + path or p.path.startswith("/" + path + "/")):
                return site
        # Subdomínios contam: o Senado publica em www12.senado.leg.br.
        elif p.hostname == domain or (p.hostname or "").endswith("." + domain):
            return site
    return None


# Conteúdo excepcionalmente relevante (nota alta o bastante) dispensa veículo
# de referência — regulação prevalece sobre a fonte, não só o contrário.
LIMIAR_PRIORIDADE_CONTEUDO = 90


def prioritario(item, prioridade):
    return bool(fonte_prioritaria(item)) or (isinstance(prioridade, int) and prioridade >= LIMIAR_PRIORIDADE_CONTEUDO)


# Termos que enriquecem a busca por veículo sem estarem nas keywords do tópico.
EXTRA_CONSULTA = {
    "data_center": ['"ReData"', '"data centre"', '"data centres"'],
    "baterias": ['"BYD"', '"EVE Energy"', '"Gotion"', '"Hithium"', '"sodium ion"', '"solid state"', '"battery storage"', '"leilão de reserva de capacidade"', '"armazenamento em baterias"'],
    "carbono": ['"carbon credit"', '"carbon credits"', '"carbon pricing"'],
}


def _limitar_janela(url):
    """Restringe buscas temáticas do Google às 72 h que o e-mail aceita.

    Sem o filtro, o Google devolve até 100 resultados de qualquer época: a maior
    parte expirava na fila sem chance de envio e ocupava vagas de matérias recentes.
    """
    partes = urlsplit(url)
    if partes.netloc != "news.google.com" or not partes.path.startswith("/rss/search"):
        return url
    params = dict(parse_qsl(partes.query, keep_blank_values=True))
    if "when:" in params.get("q", ""):
        return url
    params["q"] = params.get("q", "") + " when:3d"
    return urlunsplit(partes._replace(query=urlencode(params)))


def configuracao_email(topicos):
    config = deepcopy(topicos)
    for topic, cfg in config.items():
        # O Telegram usa TOPICOS diretamente e continua sem este filtro.
        for feed in cfg["feeds"]:
            feed["url"] = _limitar_janela(feed["url"])
        extra = EXTRA_CONSULTA.get(topic, [])
        keywords = list(dict.fromkeys([*cfg["keywords"], *[word.strip('"') for word in extra]]))
        cfg["keywords"] = keywords
        termos = "(" + " OR ".join('"' + word + '"' for word in keywords) + ")"
        for site in FONTES:
            br = veiculo_brasileiro(site)
            params = {"q": f"site:{site} {termos} when:3d", "hl": "pt-BR" if br else "en-US",
                      "gl": "BR" if br else "US", "ceid": "BR:pt-BR" if br else "US:en"}
            cfg["feeds"].append({"url": "https://news.google.com/rss/search?" + urlencode(params),
                                 "origem": "BR" if br else "INT", "veiculo_monitorado": NOMES[site]})
    return config
