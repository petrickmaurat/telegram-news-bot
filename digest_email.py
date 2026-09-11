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
# Precisa ser maior que o volume real de candidatos (hoje ~270 em data
# center, ~180 em carbono), senão o corte vira um filtro por ORDEM DOS
# FEEDS em vez de relevância — os 2 primeiros feeds (DCD+DCK) sozinhos
# já passam de 70 itens e empurram Valor/FT/Reuters/etc para fora.
MAX_CANDIDATOS_POR_TOPICO = 400
LIMITE_TEXTO_ARTIGO = 3000

ESTADO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digest_enviados.json")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
EMAIL_REMETENTE = os.environ.get("EMAIL_REMETENTE")
EMAIL_DESTINO = os.environ.get("EMAIL_DESTINO")

# Veículos de referência, em duas camadas. A IA deve SEMPRE preferir um
# candidato do Nível 1; só descer para o Nível 2 (ou abaixo) se não
# houver nenhuma opção relevante nos veículos de maior peso.
VEICULOS_NIVEL_1 = (
    "Valor Econômico, Folha de S.Paulo, O Estado de S. Paulo (Estadão), O Globo, "
    "Brazil Journal, Exame, Poder360, CNN Brasil, InfoMoney, "
    "Financial Times, The Wall Street Journal, The New York Times, The Washington Post, "
    "The Economist, Reuters, Bloomberg, Bloomberg Línea, Politico, Axios"
)
VEICULOS_NIVEL_2 = (
    "MegaWhat, epbr, CanalEnergia, Broadcast, NeoFeed, Pipeline Valor, Agência Eixos, "
    "Brasil Energia, UOL, Money Times, S&P Global, Carbon Pulse, Argus Media, ICIS, "
    "Carbon Brief, Ecosystem Marketplace, Utility Dive, Canary Media, Data Center Dynamics, "
    "Data Center Frontier, The Information, Semafor, CNBC"
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

PROMPT_SELECAO = """Você monta um informativo executivo sobre {rotulo}, para um leitor
que quer ler poucas notícias, mas as mais importantes e de fontes confiáveis.

Selecione, desta lista de candidatos:
- ATÉ {max_br} notícias sobre o BRASIL (bucket "BR")
- ATÉ {max_us} notícia sobre EUA / exterior (bucket "US")

REGRA DE VEÍCULO (aplique ANTES dos critérios de conteúdo abaixo): SEMPRE prefira uma
notícia publicada por um destes veículos de Nível 1, se houver alguma relevante:
{veiculos_1}
Só use um veículo de Nível 2 nesta lista se não houver NENHUMA opção relevante de
Nível 1 para aquele bucket:
{veiculos_2}
Um veículo fora das duas listas (pouco conhecido, blog, release corporativo, portal
regional pequeno) só deve ser escolhido em ÚLTIMO caso, se não houver absolutamente
nada relevante nos níveis 1 e 2 — e mesmo assim, prefira sempre a opção mais robusta.

Entre candidatos do mesmo nível de veículo, desempate pelos critérios de conteúdo,
do maior para o menor peso:
1. MONTANTE FINANCEIRO envolvido (investimento, aporte, contrato, financiamento, multa,
   valor de mercado). Quanto maior o valor, maior a prioridade.
2. IMPACTO REGULATÓRIO E POLÍTICO: nova regra, decisão de agência (ANEEL, ONS, MME, CVM,
   Ibama...), lei, medida provisória, disputa judicial, posição de governo.
3. {foco_setorial}

Descarte: duplicatas (mesmo fato contado por veículos diferentes — escolha só a melhor
fonte), itens que não são sobre {rotulo}, agenda de evento, conteúdo promocional sem
fato novo.

Candidatos (índice | origem | veículo | título — trecho):
{lista}

Responda APENAS com um array JSON, sem texto antes ou depois, ordenado do mais para o
menos relevante:
[{{"indice": 0, "bucket": "BR"}}, {{"indice": 7, "bucket": "US"}}]
"""

PROMPT_RESUMO = """Escreva, para cada matéria abaixo, UM PARÁGRAFO CURTO (2 a 3 frases,
no máximo ~55 palavras) em português do Brasil, em tom jornalístico, direto e atraente
para quem só vai ler esse parágrafo (sem clicar na matéria). Abra com o fato mais forte
(o número, o valor, a decisão), não com contexto genérico. Traga o número mais importante
(valor financeiro, MW, %...) e, se houver, o órgão/empresa envolvido. Sem introdução tipo
"a notícia trata de", sem floreio. Não invente nada que não esteja no texto fornecido.

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
        veiculos_1=VEICULOS_NIVEL_1,
        veiculos_2=VEICULOS_NIVEL_2,
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
            escolhidos[bucket].append(dict(candidatos[idx]))
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
# Objetivo visual: um informativo colorido e chamativo, feito pra ser
# LIDO — não só escaneado. Cartão central de 600px sobre fundo cinza
# claro. Cada tópico abre com uma FAIXA COLORIDA CHEIA (não só uma
# borda fina), com ícone + nome do tópico em branco — dá pra
# identificar Data Centers x Carbono batendo o olho, sem precisar ler.
# Dentro de cada tópico, "🇧🇷 Brasil" e "🌎 Exterior" como rótulos.
# Cada notícia é um cartão com: selo do veículo (pílula colorida),
# manchete grande e em negrito, um parágrafo de resumo em fonte maior
# e mais escura que o normal (para chamar atenção mesmo sendo curto —
# 2-3 frases, ver PROMPT_RESUMO), e um botão colorido "Ler matéria
# completa →". Nada de metadado técnico (motivo da IA etc.) aparece
# pro leitor. Ícones são emoji (não imagem) — renderizam em qualquer
# cliente de e-mail sem depender de imagem externa bloqueada. Estilos
# todos inline (compatível com Gmail/Outlook/Apple Mail).
# ----------------------------------------------------------------------

TEMA = {
    "data_center": {"cor": "#4338ca", "cor_clara": "#eef2ff", "icone": "🖥️"},
    "carbono": {"cor": "#047857", "cor_clara": "#ecfdf5", "icone": "🌱"},
}


def _card_noticia(item: dict, tema: dict) -> str:
    cor, cor_clara = tema["cor"], tema["cor_clara"]
    return f"""
      <tr><td style="padding:0 0 14px">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border:1px solid #edf0f3;border-left:5px solid {cor};
                      border-radius:4px 10px 10px 4px">
          <tr><td style="padding:16px 18px 18px">
            <span style="display:inline-block;font-size:11px;font-weight:800;letter-spacing:.4px;
                  text-transform:uppercase;color:{cor};background:{cor_clara};padding:4px 11px;
                  border-radius:999px">{item['fonte']}</span>
            <div style="margin:11px 0 7px">
              <a href="{item['link']}" style="font-size:18px;line-height:1.35;font-weight:800;
                    color:#111827;text-decoration:none">{item['titulo']}</a>
            </div>
            <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#1f2937;font-weight:500">
              {item['resumo_final']}</p>
            <a href="{item['link']}" style="display:inline-block;font-size:13px;font-weight:800;
                  color:#ffffff;text-decoration:none;background:{cor};padding:8px 16px;
                  border-radius:999px">Ler matéria completa &rarr;</a>
          </td></tr>
        </table>
      </td></tr>"""


def _secao_topico(topico: str, grupos: dict) -> str:
    tema = TEMA[topico]
    rotulo = TOPICOS[topico]["rotulo"]
    total = len(grupos["BR"]) + len(grupos["US"])
    partes = [
        f"""
        <tr><td style="padding:28px 0 14px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr><td style="background:{tema['cor']};border-radius:10px;padding:15px 18px">
              <span style="font-size:21px;vertical-align:middle">{tema['icone']}</span>
              <span style="font-size:18px;font-weight:800;color:#ffffff;vertical-align:middle;padding-left:8px">
                {rotulo}</span>
              <span style="font-size:12px;color:#ffffff;opacity:.8;vertical-align:middle">
                &nbsp;· {total} notícia(s)</span>
            </td></tr>
          </table>
        </td></tr>"""
    ]
    rotulos_bucket = {"BR": "🇧🇷&nbsp; Brasil", "US": "🌎&nbsp; Exterior"}
    for bucket in ("BR", "US"):
        if not grupos[bucket]:
            continue
        partes.append(
            f"""<tr><td style="padding:6px 0 10px 2px">
              <span style="font-size:12px;font-weight:800;letter-spacing:.4px;text-transform:uppercase;
                    color:{tema['cor']}">{rotulos_bucket[bucket]}</span>
            </td></tr>"""
        )
        partes.extend(_card_noticia(item, tema) for item in grupos[bucket])
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
             style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;
                    box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden">
        <tr><td style="background:linear-gradient(135deg,#4338ca,#047857);padding:26px 32px">
          <div style="font-size:11px;font-weight:800;letter-spacing:2px;text-transform:uppercase;
                color:rgba(255,255,255,.8)">
            🖥️ &nbsp;Panorama diário&nbsp; 🌱
          </div>
          <h1 style="margin:6px 0 2px;font-size:22px;line-height:1.3;color:#ffffff;
                font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
            Data Centers &amp; Mercado de Carbono
          </h1>
          <div style="font-size:13px;color:rgba(255,255,255,.85)">{momento}</div>
        </td></tr>
        <tr><td style="padding:22px 32px 8px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
            {secoes}
          </table>
        </td></tr>
        <tr><td style="padding:24px 32px 32px;border-top:1px solid #edf0f3">
          <p style="margin:0;font-size:11px;line-height:1.6;color:#9ca3af">
            Seleção automática a partir de feeds RSS e Google Notícias, priorizando veículos de
            referência, montante financeiro, impacto regulatório/político e ângulo de setor
            elétrico. Filtro e resumo por Claude Haiku.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def enviar_email(assunto: str, html: str) -> None:
    # EMAIL_DESTINO aceita um endereço só, uma lista de distribuição, ou
    # vários endereços separados por vírgula/ponto-e-vírgula.
    destinatarios = [
        {"email": e.strip()} for e in re.split(r"[,;]", EMAIL_DESTINO) if e.strip()
    ]
    resposta = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
            "accept": "application/json",
        },
        json={
            "sender": {"name": "Panorama DC & Carbono", "email": EMAIL_REMETENTE},
            "to": destinatarios,
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
