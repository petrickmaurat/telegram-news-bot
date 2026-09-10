"""
Digest por e-mail — roda 2x ao dia (7h e 18h BRT).

Fluxo:
  1. Lê os feeds dos dois tópicos (data center e mercado de carbono).
  2. Claude Haiku SELECIONA, por tópico, até 3 notícias do Brasil e 1
     do exterior, ranqueadas por relevância (ver PROMPT_SELECAO).
  3. Claude Haiku RESUME cada notícia escolhida em um parágrafo.
  4. Monta um e-mail HTML bem diagramado (ver montar_html) e envia
     pela API da Brevo.

Variáveis de ambiente (Secrets do repositório):
  ANTHROPIC_API_KEY  - console.anthropic.com
  BREVO_API_KEY      - Brevo > SMTP & API > API Keys
  EMAIL_REMETENTE    - endereço verificado na Brevo como remetente
  EMAIL_DESTINO      - para quem enviar
"""

import datetime
import json
import os
import re
import sys

import anthropic
import requests
import trafilatura

from common import TOPICOS, coletar_itens_novos, resolver_link_google_news, salvar_cache_google

MODELO = "claude-haiku-4-5"
MAX_BR = 3
MAX_US = 1
MAX_CANDIDATOS_POR_TOPICO = 70
LIMITE_TEXTO_ARTIGO = 3000

ESTADO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digest_enviados.json")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
EMAIL_REMETENTE = os.environ.get("EMAIL_REMETENTE")
EMAIL_DESTINO = os.environ.get("EMAIL_DESTINO")

# Veículos de referência — a IA dá mais peso a notícias publicadas
# por estes; veículo pequeno, blog ou release corporativo pesa menos.
VEICULOS_PRIORITARIOS = (
    "Valor Econômico, Folha de S.Paulo, O Estado de S. Paulo (Estadão), Brazil Journal, "
    "MegaWhat, epbr, CanalEnergia, Broadcast, Poder360, InfoMoney, NeoFeed, Pipeline, "
    "Exame, O Globo, CNN Brasil, Reuters, Bloomberg, Bloomberg Línea, Financial Times, "
    "The Washington Post, The Wall Street Journal, The New York Times, The Economist, "
    "Politico, S&P Global, Carbon Pulse, Utility Dive, Data Center Dynamics, "
    "Data Center Frontier, The Information, Canary Media, Agência eixos, Brasil Energia"
)

FOCO_SETORIAL = {
    "data_center": (
        "Dentro de data centers, priorize o ângulo do SETOR ELÉTRICO: demanda de energia, "
        "conexão à rede básica, contratação de energia, consumo, carga, subestação, "
        "impacto no sistema elétrico e nas tarifas."
    ),
    "carbono": (
        "Priorize o SETOR ELÉTRICO: geração (térmicas, hidrelétricas, renováveis), matriz "
        "elétrica, leilões, descarbonização da geração, impacto de créditos/precificação de "
        "carbono sobre o setor de energia."
    ),
}

PROMPT_SELECAO = """Você monta um informativo executivo sobre {rotulo}.

Selecione, desta lista de candidatos:
- ATÉ {max_br} notícias sobre o BRASIL (bucket "BR")
- ATÉ {max_us} notícia sobre EUA / exterior (bucket "US")

Critérios de relevância, do MAIOR para o menor peso:
1. MONTANTE FINANCEIRO envolvido (investimento, aporte, contrato, financiamento, multa,
   valor de mercado). Quanto maior o valor, maior a prioridade.
2. IMPACTO REGULATÓRIO E POLÍTICO: nova regra, decisão de agência (ANEEL, ONS, MME, CVM,
   Ibama...), lei, medida provisória, disputa judicial, posição de governo.
3. {foco_setorial}
4. VEÍCULO que publicou. Dê mais peso a veículos de referência: {veiculos}.
   Veículo pequeno/desconhecido, blog ou release promocional pesa menos.

Prioridade é critério de ORDENAÇÃO, não de exclusão: uma notícia fraca em um critério
mas forte em outro (ex.: montante financeiro muito alto) pode e deve entrar.

Descarte: duplicatas (mesmo fato), itens que não são sobre {rotulo}, agenda de evento,
conteúdo meramente promocional sem fato novo.

Candidatos (índice | origem | veículo | título — trecho):
{lista}

Responda APENAS com um array JSON, sem texto antes ou depois, ordenado do mais para o
menos relevante:
[{{"indice": 0, "bucket": "BR", "motivo": "aporte de R$ X / decisão da ANEEL / ..."}}]
"""

PROMPT_RESUMO = """Escreva, para cada matéria abaixo, UM PARÁGRAFO (4 a 6 frases) em
português do Brasil, em tom jornalístico e objetivo. O parágrafo deve trazer: o fato
central, os valores financeiros envolvidos, as empresas e órgãos citados, e o impacto
regulatório, político ou para o setor elétrico. Seja específico com números, nomes e
prazos. Não invente nada que não esteja no texto fornecido.

{blocos}

Responda APENAS com um array JSON, sem texto antes ou depois:
[{{"indice": 0, "resumo": "..."}}, {{"indice": 1, "resumo": "..."}}]
"""


def carregar_estado() -> set:
    if os.path.exists(ESTADO_FILE):
        with open(ESTADO_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def salvar_estado(estado: set) -> None:
    with open(ESTADO_FILE, "w", encoding="utf-8") as f:
        json.dump(list(estado), f, ensure_ascii=False, indent=2)


def limpar_html(texto: str) -> str:
    return re.sub(r"<[^>]+>", "", texto or "").strip()


def extrair_json(texto: str):
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", texto or "", re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _texto_modelo(resposta) -> str:
    return "".join(b.text for b in resposta.content if b.type == "text")


def selecionar(cliente, topico: str, candidatos: list) -> dict:
    """Devolve {'BR': [...], 'US': [...]} com os itens escolhidos, já
    ordenados por relevância. Fallback: primeiros itens de cada bucket."""
    candidatos = candidatos[:MAX_CANDIDATOS_POR_TOPICO]
    linhas = [
        f"{i} | {c['origem']} | {c['fonte']} | {c['titulo']} — {limpar_html(c['resumo'])[:180]}"
        for i, c in enumerate(candidatos)
    ]
    prompt = PROMPT_SELECAO.format(
        rotulo=TOPICOS[topico]["rotulo"],
        max_br=MAX_BR,
        max_us=MAX_US,
        foco_setorial=FOCO_SETORIAL[topico],
        veiculos=VEICULOS_PRIORITARIOS,
        lista="\n".join(linhas),
    )
    escolhidos = {"BR": [], "US": []}
    try:
        resposta = cliente.messages.create(
            model=MODELO, max_tokens=1500, messages=[{"role": "user", "content": prompt}]
        )
        dados = extrair_json(_texto_modelo(resposta)) or []
        for entrada in dados:
            idx = entrada.get("indice")
            bucket = entrada.get("bucket")
            if not isinstance(idx, int) or not 0 <= idx < len(candidatos):
                continue
            if bucket not in escolhidos:
                continue
            limite = MAX_BR if bucket == "BR" else MAX_US
            if len(escolhidos[bucket]) >= limite:
                continue
            item = dict(candidatos[idx])
            item["motivo"] = entrada.get("motivo", "")
            escolhidos[bucket].append(item)
    except Exception as erro:
        print(f"Seleção por IA falhou em '{topico}' ({erro}); usando fallback.")

    if not escolhidos["BR"] and not escolhidos["US"]:
        escolhidos["BR"] = [c for c in candidatos if c["origem"] == "BR"][:MAX_BR]
        escolhidos["US"] = [c for c in candidatos if c["origem"] == "INT"][:MAX_US]
    return escolhidos


def buscar_texto_artigo(link: str) -> str:
    try:
        baixado = trafilatura.fetch_url(link)
        if baixado:
            texto = trafilatura.extract(baixado, include_comments=False, include_tables=False)
            if texto:
                return texto[:LIMITE_TEXTO_ARTIGO]
    except Exception as erro:
        print(f"Não consegui extrair texto de {link}: {erro}")
    return ""


def resumir(cliente, itens: list) -> None:
    """Adiciona 'resumo_final' (um parágrafo) em cada item."""
    blocos = []
    for i, item in enumerate(itens):
        corpo = buscar_texto_artigo(item["link"]) or limpar_html(item["resumo"])
        blocos.append(f"### {i}. {item['titulo']} ({item['fonte']})\n{corpo}")

    por_indice = {}
    try:
        resposta = cliente.messages.create(
            model=MODELO,
            max_tokens=6000,
            messages=[{"role": "user", "content": PROMPT_RESUMO.format(blocos="\n\n".join(blocos))}],
        )
        for entrada in extrair_json(_texto_modelo(resposta)) or []:
            if "indice" in entrada and "resumo" in entrada:
                por_indice[entrada["indice"]] = entrada["resumo"]
    except Exception as erro:
        print(f"Resumo por IA falhou ({erro}); usando o texto do feed.")

    for i, item in enumerate(itens):
        item["resumo_final"] = por_indice.get(i) or limpar_html(item["resumo"]) or item["titulo"]


# ----------------------------------------------------------------------
# Construção do e-mail
#
# Objetivo visual: um informativo executivo limpo e agradável de ler no
# celular ou no desktop. Cartão central de 600px sobre fundo cinza claro;
# tipografia sans-serif do sistema; hierarquia clara (kicker → título →
# data). Cada tópico é uma seção com cor de destaque própria (índigo para
# data center, verde para carbono) e uma faixa lateral. Dentro da seção,
# subtítulos "Brasil" e "Exterior". Cada notícia é um cartão com: nome do
# veículo em maiúsculas na cor de destaque, manchete clicável em negrito,
# um parágrafo de resumo, e um link "Ler no <veículo> →". Estilos todos
# inline (compatível com Gmail/Outlook/Apple Mail), sem CSS externo, sem
# imagens externas.
# ----------------------------------------------------------------------

TEMA = {
    "data_center": {"cor": "#4f46e5", "cor_clara": "#eef2ff"},
    "carbono": {"cor": "#059669", "cor_clara": "#ecfdf5"},
}


def _card_noticia(item: dict, cor: str) -> str:
    motivo = (
        f'<span style="color:#9ca3af"> · {item["motivo"]}</span>' if item.get("motivo") else ""
    )
    return f"""
      <tr><td style="padding:20px 0;border-top:1px solid #edf0f3">
        <div style="font-size:11px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:{cor}">
          {item['fonte']}{motivo}
        </div>
        <div style="margin:6px 0 8px">
          <a href="{item['link']}" style="font-size:17px;line-height:1.35;font-weight:700;color:#111827;text-decoration:none">
            {item['titulo']}</a>
        </div>
        <p style="margin:0 0 10px;font-size:14px;line-height:1.65;color:#374151">{item['resumo_final']}</p>
        <a href="{item['link']}" style="font-size:13px;font-weight:600;color:{cor};text-decoration:none">
          Ler no {item['fonte']} &rarr;</a>
      </td></tr>"""


def _secao_topico(topico: str, grupos: dict) -> str:
    tema = TEMA[topico]
    rotulo = TOPICOS[topico]["rotulo"]
    total = len(grupos["BR"]) + len(grupos["US"])
    partes = [
        f"""
        <tr><td style="padding:34px 0 8px">
          <div style="border-left:4px solid {tema['cor']};padding-left:12px">
            <span style="font-size:19px;font-weight:800;color:#111827">{rotulo}</span>
            <span style="font-size:13px;color:#9ca3af"> &nbsp;{total} notícia(s)</span>
          </div>
        </td></tr>"""
    ]
    for bucket, titulo in (("BR", "Brasil"), ("US", "Exterior")):
        if not grupos[bucket]:
            continue
        partes.append(
            f"""<tr><td style="padding:16px 0 0">
              <div style="font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
                    color:{tema['cor']};background:{tema['cor_clara']};display:inline-block;
                    padding:3px 10px;border-radius:4px">{titulo}</div>
            </td></tr>"""
        )
        partes.extend(_card_noticia(item, tema["cor"]) for item in grupos[bucket])
    return "".join(partes)


def montar_html(selecao: dict, momento: str) -> str:
    secoes = "".join(
        _secao_topico(t, selecao[t])
        for t in ("data_center", "carbono")
        if selecao.get(t) and (selecao[t]["BR"] or selecao[t]["US"])
    )
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#eef1f5">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5">
    <tr><td align="center" style="padding:28px 12px">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;
                    box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden">
        <tr><td style="padding:32px 32px 0">
          <div style="font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#9ca3af">
            Panorama diário
          </div>
          <h1 style="margin:6px 0 2px;font-size:22px;line-height:1.3;color:#111827;
                font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
            Data Centers &amp; Mercado de Carbono
          </h1>
          <div style="font-size:13px;color:#9ca3af">{momento}</div>
        </td></tr>
        <tr><td style="padding:0 32px 8px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
            {secoes}
          </table>
        </td></tr>
        <tr><td style="padding:24px 32px 32px;border-top:1px solid #edf0f3">
          <p style="margin:0;font-size:11px;line-height:1.6;color:#9ca3af">
            Seleção automática a partir de feeds RSS e Google Notícias, priorizando montante
            financeiro, impacto regulatório/político, ângulo de setor elétrico e veículos de
            referência. Filtro e resumo por Claude Haiku.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def enviar_email(assunto: str, html: str) -> None:
    resposta = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
            "accept": "application/json",
        },
        json={
            "sender": {"name": "Panorama DC & Carbono", "email": EMAIL_REMETENTE},
            "to": [{"email": EMAIL_DESTINO}],
            "subject": assunto,
            "htmlContent": html,
        },
        timeout=30,
    )
    if not resposta.ok:
        sys.exit(f"Falha ao enviar e-mail: {resposta.status_code} {resposta.text}")
    print("E-mail enviado.")


def rodar_digest() -> None:
    estado = carregar_estado()
    itens = coletar_itens_novos(estado, resolver=False)
    links_coletados = [item["link"] for item in itens]

    por_topico = {t: [] for t in TOPICOS}
    for item in itens:
        por_topico[item["topico"]].append(item)
    print({t: len(v) for t, v in por_topico.items()})

    cliente = anthropic.Anthropic()
    selecao = {t: selecionar(cliente, t, por_topico[t]) for t in TOPICOS if por_topico[t]}

    selecionados = [it for grupos in selecao.values() for it in (grupos["BR"] + grupos["US"])]
    if selecionados:
        for it in selecionados:
            it["link"] = resolver_link_google_news(it["link"])
        salvar_cache_google()
        resumir(cliente, selecionados)

        agora = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
        periodo = "manhã" if agora.hour < 14 else "tarde"
        momento = agora.strftime(f"%d/%m/%Y · {periodo}")
        assunto = f"Panorama Data Centers & Carbono — {agora.strftime('%d/%m')} ({periodo})"
        enviar_email(assunto, montar_html(selecao, momento))
    else:
        print("Nada relevante selecionado; não vou enviar e-mail.")

    for link in links_coletados:
        estado.add(link)
    salvar_estado(estado)


if __name__ == "__main__":
    faltando = [
        nome
        for nome, valor in (
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("BREVO_API_KEY", BREVO_API_KEY),
            ("EMAIL_REMETENTE", EMAIL_REMETENTE),
            ("EMAIL_DESTINO", EMAIL_DESTINO),
        )
        if not valor
    ]
    if faltando:
        sys.exit(f"Faltam variáveis de ambiente: {', '.join(faltando)}")
    rodar_digest()
