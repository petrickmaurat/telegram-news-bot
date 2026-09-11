"""Catálogo e consultas por veículo, exclusivos do e-mail."""
from copy import deepcopy
from urllib.parse import urlencode, urlsplit
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
}
FONTES = sorted({*DOMINIOS[1], *DOMINIOS[2], "agenciainfra.com", "gov.br/mme"})


def fonte_prioritaria(item):
    p = urlsplit(canonica(item.get("link", "")))
    for site in FONTES:
        domain, _, path = site.partition("/")
        if p.hostname == domain and (not path or p.path == "/" + path or p.path.startswith("/" + path + "/")):
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


def configuracao_email(topicos):
    config = deepcopy(topicos)
    for topic, cfg in config.items():
        extra = EXTRA_CONSULTA.get(topic, [])
        keywords = list(dict.fromkeys([*cfg["keywords"], *[word.strip('"') for word in extra]]))
        cfg["keywords"] = keywords
        termos = "(" + " OR ".join('"' + word + '"' for word in keywords) + ")"
        for site in FONTES:
            br = site.endswith(".br") or site in ("gov.br/mme", "agenciainfra.com")
            params = {"q": f"site:{site} {termos} when:3d", "hl": "pt-BR" if br else "en-US",
                      "gl": "BR" if br else "US", "ceid": "BR:pt-BR" if br else "US:en"}
            cfg["feeds"].append({"url": "https://news.google.com/rss/search?" + urlencode(params),
                                 "origem": "BR" if br else "INT", "veiculo_monitorado": NOMES[site]})
    return config
