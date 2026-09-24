"""Digest executado por uma Claude Code Routine, sem API de IA.

O script cuida de coleta, estado e validação. A Routine só toma decisões
editoriais e grava JSON nos formatos documentados em ROUTINE_V2_INSTRUCTIONS.md.
A Routine nunca envia e-mail: o comando ``send`` roda no GitHub Actions, depois
que o resultado validado chega à branch v2-claude-routines.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

import common
import requests
import trafilatura
from common import TOPICOS, coletar_itens_novos, resolver_link_google_news, salvar_cache_google
from digest_email import FOCO_SETORIAL, VAGAS, montar_html
from email_articles import enriquecer_fila
from email_language import idioma_permitido
from email_policy import IDADE_MAXIMA, google_pendente, recente
from email_sources import (LIMIAR_PRIORIDADE_CONTEUDO, configuracao_email, fonte_maxima,
                           fonte_prioritaria)
from email_topic import verificar_tema
from reliability import canonica, carregar_json, identidades, salvar_json


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "routine_v2_state.json"
GOOGLE_CACHE_FILE = ROOT / "routine_v2_google_cache.json"
INPUT_FILE = ROOT / "routine_v2_input.json"
WORK_DIR = ROOT / "routine_v2_work"
REQUEST_FILE = WORK_DIR / "ranking_request.json"
RANKING_RESPONSE_FILE = WORK_DIR / "ranking_response.json"
SUMMARY_REQUEST_FILE = WORK_DIR / "summary_request.json"
SUMMARY_RESPONSE_FILE = WORK_DIR / "summary_response.json"
REPORT_FILE = ROOT / "routine_v2_report.json"
PREVIEW_FILE = ROOT / "routine_v2_preview.html"
PREFLIGHT_FILE = ROOT / "routine_v2_preflight.json"
TRIGGER_FILE = ROOT / "routine_v2_trigger.txt"
COLLECTION_REQUEST_FILE = WORK_DIR / "coleta_solicitada.json"
BRANCH = "v2-claude-routines"
WAIT_POLL_SECONDS = 30
BATCH_DIR = WORK_DIR / "lotes"
SHORTLIST_FILE = WORK_DIR / "finalistas.json"
# Cada lote é avaliado por um subagente com contexto próprio. Um agente único
# acumulava todos os candidatos no contexto e reprocessava tudo a cada passo.
EVALUATION_BATCH_SIZE = 60
SHORTLIST_PER_BUCKET = 15
# Notas de subagentes diferentes não são calibradas entre si. Como no torneio da
# V1, os melhores de cada lote sempre chegam à rodada final.
SHORTLIST_PER_BATCH = 3
SHORTLIST_EXCERPT = 200
# Fatos já enviados ficam na memória para não repetir o mesmo acontecimento com
# outro link, como a V1 fazia comparando as manchetes enviadas.
SENT_FACTS_DAYS = 7
# Mesmas regras editoriais do PROMPT da V1 (email_ranking.py). O foco ordena as
# notas; ele nunca restringe o que pertence ao tema.
BATCH_RULES = (
    "Avalie cada candidato somente para o tópico deste lote. PRIMEIRO determine se o "
    "assunto principal tem relação DIRETA com o tópico, comprovada no título ou trecho; "
    "não invente relações potenciais. Menção incidental, notícias apenas relacionadas e "
    "boletins misturando assuntos são fora_tema. Uma notícia de energia não vira notícia de "
    "data centers por energia ser necessária a data centers: o fim da recuperação judicial da "
    "Light é fora_tema em data centers; um investimento em baterias sem ligação explícita a data "
    "centers pertence a baterias. Transmissão para conectar data centers é elegível. "
    "Nome da fonte nunca altera o tema. "
    "O tema é amplo: data centers incluem infraestrutura, tecnologia, energia, regulação, leis, "
    "tributação, incentivos e investimentos; mercado de carbono inclui o industrial, florestal, "
    "regulado e voluntário. O FOCO serve APENAS para ordenar a prioridade: uma notícia do tema "
    "fora do foco continua elegível, com nota menor. Não rejeite por falta de valor financeiro, "
    "por ser internacional, por não tratar do setor elétrico ou por vir de veículo menor. "
    "Análises com informação substantiva são elegíveis. Regulação, leis, impacto legal e "
    "montantes financeiros aumentam a prioridade. Só rejeite: fora_tema; sem_fato_novo "
    "(agenda ou publicidade vazia); fonte_duvidosa com evidência concreta (fonte desconhecida "
    "não é prova). Rejeitados levam apenas id e decisao. Elegíveis levam bucket BR (fato "
    "ocorrido no Brasil) ou US (fato fora do Brasil; a geografia é a do fato, não a do veículo), "
    "prioridade inteira de 0 a 100 conforme o foco, e fato: identificador curto do acontecimento "
    "(ex.: catl-reduz-preco-celulas), igual para coberturas do mesmo acontecimento e diferente "
    "para empresas, decisões, etapas ou valores novos.")
# 2: regras dos lotes alinhadas à V1; avaliações anteriores refeitas.
POLICY_VERSION = 2
REQUEST_SCHEMA = 4
DECISIONS = {"elegivel", "fora_tema", "sem_fato_novo", "fonte_duvidosa"}
TOPIC_ORDER = ("data_center", "baterias", "carbono")
MIN_ARTICLE_WORDS = 80
MIN_EXCERPT_WORDS = 40
MIN_LIMITED_WORDS = 10
ARTICLE_TEXT_LIMIT = 6000
# Trecho enviado ao ranking. Candidatos já avaliados só competem pela nota salva,
# então recebem um trecho menor para desempate.
RANKING_EXCERPT_NEW = 600
RANKING_EXCERPT_CACHED = 300
DELIVERY_RETRY_SECONDS = 6 * 3600
DELIVERY_READER_VERSION = 2
PREFLIGHT_TARGETS = (
    ("google_news", "buscador", TOPICOS["data_center"]["feeds"][2]["url"]),
    ("megawhat", "rss_direto", "https://megawhat.uol.com.br/feed/"),
    ("data_center_dynamics", "rss_direto",
     "https://www.datacenterdynamics.com/en/rss/"),
)


def clean(text):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def save_compact_json(path, value):
    """Grava JSON sem indentação, com um registro por linha.

    A Routine paga por caractere lido: a indentação do salvar_json ocupava cerca
    de 15% do pedido. Um registro por linha mantém o arquivo legível pelo Read.
    """
    def dump(data):
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    lines = []
    for key in sorted(value):
        data, name = value[key], dump(key)
        if isinstance(data, list) and data:
            lines.append(name + ":[\n" + ",\n".join(dump(x) for x in data) + "\n]")
        elif isinstance(data, dict) and data:
            lines.append(name + ":{\n" + ",\n".join(
                dump(k) + ":" + dump(data[k]) for k in sorted(data)) + "\n}")
        else:
            lines.append(name + ":" + dump(data))
    text = "{\n" + ",\n".join(lines) + "\n}\n"
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def is_maximum(candidate):
    return candidate.get("fonte_maxima") is True


def keep_in_state(item, now):
    """Mantém só o que ainda pode virar candidato: dentro da janela ou sem data recente."""
    return recente(item, now) or (item.get("publicado_em") is None
                                  and now - item.get("_v2_seen_at", now) <= 7 * 86400)


def domain(link):
    host = urlsplit(link).hostname or ""
    return host[4:] if host.startswith("www.") else host


def item_key(item):
    return canonica(item.get("link", "")) or hashlib.sha256(
        (item.get("titulo", "") + item.get("fonte", "")).encode("utf-8")).hexdigest()


def candidate_id(topic, item):
    return hashlib.sha256((topic + "\0" + item_key(item)).encode("utf-8")).hexdigest()[:20]


def evaluation_signature(topic, item):
    payload = [POLICY_VERSION, topic, FOCO_SETORIAL[topic], {
        "titulo": item.get("titulo"), "fonte": item.get("fonte"),
        "link": canonica(item.get("link", "")), "resumo": clean(item.get("resumo", ""))[:1200]}]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def initial_state():
    state = {"version": 1, "items": {}, "evaluations": {}, "sent": []}
    state["sent"] = sorted(set(carregar_json(ROOT / "digest_enviados.json", [])))
    now = time.time()
    for record in carregar_json(ROOT / "digest_fila.json", {}).values():
        item = record.get("item", {})
        if item.get("titulo") and (recente(item, now) or
                (item.get("publicado_em") is None and now - record.get("criado", now) <= 7 * 86400)):
            item = dict(item)
            item["_v2_seen_at"] = record.get("criado", now)
            state["items"][item_key(item)] = item
    return state


def load_state():
    state = carregar_json(STATE_FILE, None)
    return state if isinstance(state, dict) else initial_state()


def activate_google_cache():
    """Aponta o módulo comum para o cache da V2 antes de qualquer coleta."""
    source = GOOGLE_CACHE_FILE if GOOGLE_CACHE_FILE.exists() else ROOT / "google_cache.json"
    common._cache_google = carregar_json(source, {})
    common._CACHE_GOOGLE_FILE = str(GOOGLE_CACHE_FILE)


def merge_item(existing, incoming):
    if not existing:
        incoming = dict(incoming)
        incoming.setdefault("_v2_seen_at", time.time())
        return incoming
    merged = dict(existing)
    merged["aliases"] = sorted(identidades(existing) | identidades(incoming))
    merged["topicos"] = sorted(set(existing.get("topicos", [])) | set(incoming.get("topicos", [])))
    merged["feeds_origem"] = sorted(set(existing.get("feeds_origem", [])) | set(incoming.get("feeds_origem", [])))
    if len(clean(incoming.get("resumo", ""))) > len(clean(existing.get("resumo", ""))):
        merged.update({k: incoming[k] for k in ("titulo", "fonte", "resumo") if k in incoming})
    if merged.get("publicado_em") is None and incoming.get("publicado_em") is not None:
        merged["publicado_em"] = incoming["publicado_em"]
        merged["origem_data"] = incoming.get("origem_data", "rss")
    if incoming.get("artigo", {}).get("texto"):
        merged["artigo"] = incoming["artigo"]
    return merged


def word_count(text):
    return len(clean(text).split())


def delivery_view(item, allow_limited=False):
    """Material verificavel usado no resumo e no link final da noticia."""
    article = item.get("artigo", {})
    original = canonica(item.get("link", ""))
    resolved = canonica(article.get("link_final", ""))
    link = resolved if resolved and not google_pendente({"link": resolved}) else original
    link_ready = bool(link) and not google_pendente({"link": link})
    article_text = clean(article.get("texto", ""))[:ARTICLE_TEXT_LIMIT]
    rss_text = clean(item.get("resumo", ""))[:ARTICLE_TEXT_LIMIT]
    article_words, rss_words = word_count(article_text), word_count(rss_text)
    if article_words >= MIN_ARTICLE_WORDS:
        level, text, words = "artigo_completo", article_text, article_words
    else:
        text = article_text if article_words > rss_words else rss_text
        words = max(article_words, rss_words)
        if words >= MIN_EXCERPT_WORDS:
            level = "trecho_disponivel"
        elif allow_limited and words >= MIN_LIMITED_WORDS:
            level = "trecho_limitado"
        else:
            level = "insuficiente"
    return {"nivel": level, "palavras": words, "link_final": link,
            "link_resolvido": link_ready,
            # Relevancia e acesso ao texto sao decisoes diferentes. Com link
            # direto, o candidato pode ser ranqueado e o Claude tenta ler
            # somente os finalistas; falha de scraping nao e veto editorial.
            "selecionavel": link_ready, "texto": text}


def _fetch_delivery_article(item, now):
    """Resolve e le um candidato sem trocar sua identidade editorial."""
    article = dict(item.get("artigo", {}))
    current = delivery_view(item)
    if current["nivel"] == "artigo_completo" and current["link_resolvido"]:
        return
    # Uma mudanca no resolvedor/leitor precisa invalidar falhas antigas. Sem
    # esta versao, o cache impediria a correcao de ser testada por seis horas.
    if (article.get("leitor_versao") == DELIVERY_READER_VERSION
            and article.get("entrega_tentada_em", 0) + DELIVERY_RETRY_SECONDS > now):
        return
    article["entrega_tentada_em"] = now
    article["leitor_versao"] = DELIVERY_READER_VERSION
    original = item.get("link", "")
    try:
        resolved = resolver_link_google_news(original)
        final_link = canonica(resolved)
        if not final_link or google_pendente({"link": final_link}):
            raise ValueError("link final nao resolvido")
        article["link_final"] = final_link
        response = requests.get(final_link, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        extracted = trafilatura.extract(response.text[:2_000_000],
                                        include_comments=False, include_tables=False) or ""
        extracted = clean(extracted)[:ARTICLE_TEXT_LIMIT]
        if extracted:
            article["texto"] = extracted
        article["resultado_entrega"] = ("artigo_completo"
            if word_count(extracted) >= MIN_ARTICLE_WORDS else "texto_curto")
        article["motivo_entrega"] = "Link resolvido e pagina consultada."
    except Exception as error:
        article["resultado_entrega"] = "falha"
        article["motivo_entrega"] = str(error)[:300]
    item["artigo"] = article


def enrich_delivery_candidates(items, max_workers=12):
    """Prepara links e texto fora do Claude, com cache por item."""
    unique = list({item_key(item): item for item in items}.values())
    now = time.time()
    if not unique:
        return
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique))) as pool:
        list(pool.map(lambda item: _fetch_delivery_article(item, now), unique))


def dedupe_same_article(items):
    """Une URLs que representam o mesmo artigo; manchetes parecidas ficam separadas.

    Depois que um link do Google Noticias e resolvido, a coleta seguinte pode
    trazer a URL direta. ``aliases`` liga as duas identidades e impede que a
    mesma materia volte como candidata nova ou perca sua avaliacao em cache.
    """
    merged = {}
    alias_to_key = {}
    for item in items:
        aliases = identidades(item)
        key = next((alias_to_key[alias] for alias in aliases
                    if alias in alias_to_key), item_key(item))
        merged[key] = merge_item(merged.get(key), item)
        for alias in identidades(merged[key]) | {key}:
            alias_to_key[alias] = key
    return list(merged.values())


def admissible(item, topic, now):
    if not recente(item, now):
        return False, "fora_janela"
    if not verificar_tema(item, topic)[0]:
        return False, "sem_evidencia_tema"
    if topic == "baterias" and not idioma_permitido(item)[0]:
        return False, "idioma"
    if not canonica(item.get("link", "")):
        return False, "link_invalido"
    return True, "apto"


def preflight():
    """Confirma acesso aos dois caminhos essenciais antes da coleta completa."""
    results = []
    for name, group, url in PREFLIGHT_TARGETS:
        row = {"name": name, "group": group, "url": url, "ok": False}
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            row.update({"status_code": response.status_code,
                        "bytes": len(response.content),
                        "ok": response.ok and bool(response.content)})
        except requests.RequestException as error:
            row["error"] = f"{type(error).__name__}: {error}"
        results.append(row)
    google_ready = any(r["ok"] for r in results if r["group"] == "buscador")
    direct_ready = any(r["ok"] for r in results if r["group"] == "rss_direto")
    report = {"status": "network_ready" if google_ready and direct_ready
              else "network_failed", "generated_at": time.time(), "targets": results}
    salvar_json(PREFLIGHT_FILE, report)
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "network_ready":
        raise RuntimeError("Preflight de rede falhou; coleta completa e IA não foram executadas.")


def collection_health(sources):
    total = len(sources)
    outcomes = Counter(row.get("resultado", "desconhecido") for row in sources)
    failed = outcomes.get("falha", 0)
    return {
        "queries": total,
        "ok": outcomes.get("ok", 0),
        "failed": failed,
        "invalid": outcomes.get("rss_invalido", 0),
        "entries": sum(int(row.get("itens_rss", 0) or 0) for row in sources),
        "failure_ratio": round(failed / total, 4) if total else 0,
        "failed_examples": [row.get("fonte", row.get("consulta", ""))
                            for row in sources if row.get("resultado") == "falha"][:10],
    }


def request_sha256(request):
    """Calcula o identificador do pedido sem depender do hash já gravado nele."""
    payload = dict(request)
    payload.pop("request_sha256", None)
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def clear_work_outputs(include_request=True):
    paths = [RANKING_RESPONSE_FILE, SUMMARY_REQUEST_FILE, SUMMARY_RESPONSE_FILE,
             REPORT_FILE, PREVIEW_FILE, SHORTLIST_FILE, *BATCH_DIR.glob("*.json")]
    if include_request:
        paths.insert(0, REQUEST_FILE)
    for stale in paths:
        if stale.exists():
            stale.unlink()


def prepare(max_new_per_topic=0):
    WORK_DIR.mkdir(exist_ok=True)
    clear_work_outputs()
    # Falhas de coleta jamais podem deixar uma entrada antiga disponível à Routine.
    if INPUT_FILE.exists():
        INPUT_FILE.unlink()
    activate_google_cache()
    state = load_state()
    now = time.time()
    state["items"] = {key: item for key, item in state.get("items", {}).items()
                      if keep_in_state(item, now)}
    sent = {canonica(x) for x in state.get("sent", [])}
    sources = []
    collected = coletar_itens_novos(sent, resolver=False,
        configuracao=configuracao_email(TOPICOS), relatorio_fontes=sources,
        feed_workers=8)
    health = collection_health(sources)
    if sources and (health["ok"] == 0 or health["failure_ratio"] >= 0.8):
        report = {"status": "collection_failed", "send_enabled": False,
                  "generated_at": time.time(), "collection": health,
                  "note": "Coleta abortada antes da IA; cobertura de fontes insuficiente."}
        salvar_json(REPORT_FILE, report)
        print(json.dumps(report, ensure_ascii=False))
        raise RuntimeError("Coleta indisponível; ranking e resumos não foram executados.")
    for item in collected:
        key = item_key(item)
        state["items"][key] = merge_item(state["items"].get(key), item)
    # Matérias com data fora da janela nunca chegam ao ranking; guardá-las só
    # inflava o estado versionado e o tempo de enriquecimento.
    state["items"] = {key: item for key, item in state["items"].items()
                      if keep_in_state(item, now)}

    # Enriquece uma cópia da fila; não chama qualquer modelo de IA.
    queue = {key: {"item": item, "status": "pendente"}
             for key, item in state["items"].items()}
    enriquecer_fila(queue, resolver_link_google_news, time.time(), max_workers=8)
    items = [r["item"] for r in queue.values()]
    # A primeira passagem continua limitada a dados ausentes. A etapa de
    # entrega abaixo tentará ler todos os candidatos efetivos do ranking.
    items = dedupe_same_article(items)
    state["items"] = {item_key(item): item for item in items}
    now = time.time()
    candidates = []
    candidate_items = {}
    rejected_locally = Counter()
    truncated = {}
    sent_ids = {canonica(x) for x in state.get("sent", [])}
    for topic in TOPIC_ORDER:
        topic_items = []
        for item in items:
            if topic not in item.get("topicos", []):
                continue
            if identidades(item) & sent_ids:
                rejected_locally[(topic, "ja_enviada")] += 1
                continue
            ok, reason = admissible(item, topic, now)
            if not ok:
                rejected_locally[(topic, reason)] += 1
                continue
            topic_items.append(item)
        topic_items.sort(key=lambda i: (0 if fonte_maxima(i) else 1,
                                        0 if fonte_prioritaria(i) else 1,
                                        -i.get("publicado_em", 0), item_key(i)))
        cached_candidates, new_candidates = [], []
        for item in topic_items:
            cid = candidate_id(topic, item)
            signature = evaluation_signature(topic, item)
            cached = state.get("evaluations", {}).get(cid)
            valid_cache = cached and cached.get("assinatura") == signature
            published = item.get("publicado_em")
            candidate = {
                "id": cid, "topico": topic, "titulo": item["titulo"], "fonte": item.get("fonte", ""),
                "publicado_em": int(published) if isinstance(published, (int, float)) else None,
                "precisa_avaliar": not valid_cache,
            }
            candidate_items[cid] = item
            if valid_cache:
                candidate["avaliacao_cache"] = cached["avaliacao"]
            if valid_cache:
                if cached["avaliacao"].get("decisao") == "elegivel":
                    cached_candidates.append(candidate)
                else:
                    rejected_locally[(topic, "rejeitada_em_cache")] += 1
            else:
                new_candidates.append(candidate)
        if max_new_per_topic and len(new_candidates) > max_new_per_topic:
            truncated[topic] = {"novos_incluidos": max_new_per_topic,
                                "novos_aptos": len(new_candidates),
                                "elegiveis_em_cache": len(cached_candidates)}
            new_candidates = new_candidates[:max_new_per_topic]
        candidates.extend(cached_candidates + new_candidates)

    # A rede do GitHub prepara o material antes da Routine: resolve os links e
    # tenta ler todos os candidatos que realmente chegaram ao ranking. O cache
    # no estado evita repetir essas consultas nas execucoes seguintes.
    active_ids = {candidate["id"] for candidate in candidates}
    enrich_delivery_candidates([candidate_items[cid] for cid in active_ids])
    salvar_cache_google()
    for candidate in candidates:
        item = candidate_items[candidate["id"]]
        base_view = delivery_view(item)
        delivery_item = {**item, "link": base_view["link_final"]}
        # Marcadores só aparecem quando verdadeiros; ausência significa falso.
        for flag, value in (("fonte_maxima", fonte_maxima(delivery_item)),
                            ("fonte_prioritaria", bool(fonte_prioritaria(delivery_item)))):
            if value:
                candidate[flag] = True
        view = delivery_view(item, allow_limited=is_maximum(candidate))
        limit = RANKING_EXCERPT_NEW if candidate["precisa_avaliar"] else RANKING_EXCERPT_CACHED
        candidate["trecho"] = view["texto"][:limit]
        # selecionavel já equivale a link direto resolvido; palavras e
        # link_resolvido eram redundantes para a decisão editorial.
        candidate["leitura"] = {"nivel": view["nivel"], "selecionavel": view["selecionavel"]}
        # O domínio identifica o veículo; a URL completa custava ~11% do pedido
        # e o link final continua indo ao passo de resumos.
        if view["link_resolvido"]:
            candidate["dominio"] = domain(view["link_final"])
    reading = Counter(candidate["leitura"]["nivel"] for candidate in candidates)
    reading_by_topic = {
        topic: dict(Counter(candidate["leitura"]["nivel"] for candidate in candidates
                            if candidate["topico"] == topic))
        for topic in TOPIC_ORDER
    }

    request = {
        "schema": REQUEST_SCHEMA, "policy_version": POLICY_VERSION,
        "run_id": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": now, "pilot": True, "send_enabled": False,
        "limits": VAGAS, "topic_order": list(TOPIC_ORDER), "focus": FOCO_SETORIAL,
        "batch_size": 30, "batch_winners_per_bucket": 10,
        "candidates": candidates,
        "reading": dict(reading), "reading_by_topic": reading_by_topic,
        "local_rejections": [{"topico": topic, "resultado": reason, "quantidade": count}
                             for (topic, reason), count in sorted(rejected_locally.items())],
        "truncated": truncated, "source_queries": len(sources), "collection": health,
        # Branch que disparou a coleta: wait-input só aceita a do próprio pedido.
        "trigger": os.environ.get("GITHUB_REF_NAME", "local"),
    }
    request["request_sha256"] = request_sha256(request)
    live_evaluation_ids = {candidate_id(topic, item) for item in items
                           for topic in item.get("topicos", [])}
    state["evaluations"] = {key: value for key, value in state.get("evaluations", {}).items()
                            if key in live_evaluation_ids}
    save_compact_json(STATE_FILE, state)
    save_compact_json(REQUEST_FILE, request)
    save_compact_json(INPUT_FILE, request)
    print(json.dumps({"status": "prepared", "request": str(REQUEST_FILE),
        "candidates": len(candidates),
        "needs_evaluation": sum(c["precisa_avaliar"] for c in candidates),
        "cached": sum(not c["precisa_avaliar"] for c in candidates),
        "reading": dict(reading),
        "truncated": truncated, "send_enabled": False}, ensure_ascii=False))


def load_input(max_age_hours=6):
    """Carrega na área de trabalho a coleta feita previamente pelo GitHub Actions."""
    WORK_DIR.mkdir(exist_ok=True)
    clear_work_outputs()
    request = carregar_json(INPUT_FILE, None)
    if not isinstance(request, dict):
        raise ValueError("Entrada V2 ausente. Execute antes o workflow de preparação no GitHub.")
    if request.get("schema") != REQUEST_SCHEMA or request.get("policy_version") != POLICY_VERSION:
        raise ValueError("Entrada V2 usa schema ou política incompatível com este código.")
    if request.get("pilot") is not True or request.get("send_enabled") is not False:
        raise ValueError("Entrada V2 não está marcada como piloto seguro sem envio.")
    if request.get("request_sha256") != request_sha256(request):
        raise ValueError("Entrada V2 falhou na verificação de integridade.")
    generated_at = request.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        raise ValueError("Entrada V2 não informa quando foi gerada.")
    age_seconds = time.time() - generated_at
    if age_seconds < -300 or age_seconds > max_age_hours * 3600:
        raise ValueError(f"Entrada V2 fora da janela de {max_age_hours:g} hora(s). "
                         "Peça uma coleta nova com request-input e wait-input (passo 1).")
    if request.get("truncated"):
        raise ValueError("Entrada V2 foi truncada; o teste integral foi bloqueado.")
    collection = request.get("collection")
    if (not isinstance(collection, dict) or collection.get("queries", 0) <= 0
            or collection.get("ok", 0) <= 0 or collection.get("failure_ratio", 1) >= 0.8):
        raise ValueError("Entrada V2 não comprova uma coleta saudável.")
    if not STATE_FILE.exists():
        raise ValueError("Estado V2 correspondente à entrada não foi encontrado.")
    candidates = request.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Entrada V2 não contém uma lista válida de candidatos.")
    for candidate in candidates:
        reading = candidate.get("leitura") if isinstance(candidate, dict) else None
        if (not isinstance(reading, dict)
                or reading.get("nivel") not in
                    ("artigo_completo", "trecho_disponivel", "trecho_limitado", "insuficiente")
                or type(reading.get("selecionavel")) is not bool):
            raise ValueError("Entrada V2 contém candidato sem diagnóstico de leitura.")

    # Feeds acessiveis nao garantem links finais utilizaveis. A Routine so deve
    # consumir a franquia do Claude quando houver links diretos suficientes
    # para preencher todas as vagas; quantidade de texto nao entra nesta trava.
    limits = request.get("limits", VAGAS)
    selectable_by_topic = Counter(candidate.get("topico") for candidate in candidates
                                  if candidate.get("leitura", {}).get("selecionavel") is True)
    missing = {}
    for topic, buckets in limits.items():
        required = sum(int(value) for value in buckets.values())
        available = selectable_by_topic.get(topic, 0)
        if available < required:
            missing[topic] = {"selecionaveis": available, "necessarias": required}
    if missing:
        raise ValueError("Entrada V2 sem links diretos suficientes: "
                         + json.dumps(missing, ensure_ascii=False, sort_keys=True))

    save_compact_json(REQUEST_FILE, request)
    print(json.dumps({"status": "input_loaded", "request": str(REQUEST_FILE),
        "run_id": request.get("run_id"), "age_minutes": round(age_seconds / 60, 1),
        "candidates": len(candidates),
        "needs_evaluation": sum(bool(c.get("precisa_avaliar")) for c in candidates),
        "cached": sum(not bool(c.get("precisa_avaliar")) for c in candidates),
        "send_enabled": False}, ensure_ascii=False))


def valid_evaluation(row, candidate):
    if not isinstance(row, dict) or row.get("id") != candidate["id"] or row.get("decisao") not in DECISIONS:
        raise ValueError(f"Avaliação inválida para {candidate['id']}")
    if row["decisao"] == "elegivel":
        if row.get("bucket") not in ("BR", "US") or type(row.get("prioridade")) is not int:
            raise ValueError(f"Elegível sem geografia/nota válida: {candidate['id']}")
        if not 0 <= row["prioridade"] <= 100 or not str(row.get("fato", "")).strip():
            raise ValueError(f"Elegível com nota/fato inválido: {candidate['id']}")
        return {k: row[k] for k in ("decisao", "bucket", "prioridade", "fato")}
    return {"decisao": row["decisao"]}


def candidate_selectable(candidate):
    return candidate.get("leitura", {}).get("selecionavel") is True


def validate_ranking(response_path=None):
    response_path = Path(response_path) if response_path else RANKING_RESPONSE_FILE
    request = carregar_json(REQUEST_FILE, None)
    response = carregar_json(response_path, None)
    if not request or not response:
        raise ValueError("Execute prepare e grave a resposta de ranking antes de validar.")
    if response.get("request_sha256") != request["request_sha256"]:
        raise ValueError("A resposta pertence a outra coleta; nada foi aplicado.")
    candidates = {c["id"]: c for c in request["candidates"]}
    rows = response.get("evaluations")
    if not isinstance(rows, list) or len({r.get("id") for r in rows if isinstance(r, dict)}) != len(rows):
        raise ValueError("Avaliações ausentes ou IDs duplicados.")
    supplied = {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id") in candidates}
    required = {c["id"] for c in candidates.values() if c["precisa_avaliar"]}
    if set(supplied) != required:
        raise ValueError(f"A resposta deve cobrir exatamente os {len(required)} candidatos novos.")

    state = load_state()
    by_candidate_id = {}
    for item in state["items"].values():
        for topic in item.get("topicos", []):
            by_candidate_id[candidate_id(topic, item)] = item
    evaluations = {}
    for cid, candidate in candidates.items():
        if candidate["precisa_avaliar"]:
            evaluation = valid_evaluation(supplied[cid], candidate)
            original = by_candidate_id.get(cid)
            if original is None:
                raise ValueError("Item original de candidato nao foi localizado.")
            state.setdefault("evaluations", {})[cid] = {
                "assinatura": evaluation_signature(candidate["topico"], original),
                "avaliacao": evaluation}
        else:
            evaluation = candidate["avaliacao_cache"]
        evaluations[cid] = evaluation

    selections = response.get("selections")
    if not isinstance(selections, list) or len({s.get("id") for s in selections if isinstance(s, dict)}) != len(selections):
        raise ValueError("Seleções ausentes ou repetidas.")
    selected_ids = {s.get("id") for s in selections}
    if not selected_ids <= set(candidates):
        raise ValueError("A seleção contém candidato desconhecido.")
    duplicate_of = response.get("duplicates", {})
    if not isinstance(duplicate_of, dict):
        raise ValueError("duplicates deve ser um objeto id: id.")
    for child, parent in duplicate_of.items():
        if child not in candidates or parent not in candidates or child == parent:
            raise ValueError("Relação de duplicidade inválida.")
        if candidates[child]["topico"] != candidates[parent]["topico"]:
            raise ValueError("Duplicidade não pode atravessar tópicos.")
        if child in selected_ids or parent in duplicate_of:
            raise ValueError("Duplicidade deve apontar diretamente para a cobertura preservada.")
        if (evaluations[child].get("decisao") != "elegivel"
                or evaluations[parent].get("decisao") != "elegivel"):
            raise ValueError("Duplicidade editorial só pode relacionar coberturas elegíveis.")
        if is_maximum(candidates[child]) and not is_maximum(candidates[parent]):
            raise ValueError("Uma fonte máxima não pode ser descartada em favor de fonte comum.")

    groups = {t: {"BR": [], "US": []} for t in TOPIC_ORDER}
    for selected in selections:
        cid = selected["id"]
        candidate, evaluation = candidates[cid], evaluations[cid]
        if not evaluation or evaluation.get("decisao") != "elegivel":
            raise ValueError("Um item selecionado não foi classificado como elegível.")
        if not candidate_selectable(candidate):
            raise ValueError("Um item selecionado nao tem link direto para leitura final.")
        if selected.get("topico") != candidate["topico"] or selected.get("bucket") != evaluation["bucket"]:
            raise ValueError("Tópico/geografia da seleção diverge da avaliação.")
        groups[candidate["topico"]][evaluation["bucket"]].append(candidate)
    for topic in TOPIC_ORDER:
        for bucket in ("BR", "US"):
            chosen = groups[topic][bucket]
            limit = VAGAS[topic][bucket]
            if len(chosen) > limit:
                raise ValueError(f"Seleção excedeu vagas de {topic}/{bucket}.")
            eligible = [c for c in candidates.values() if c["topico"] == topic
                        and evaluations[c["id"]].get("decisao") == "elegivel"
                        and evaluations[c["id"]].get("bucket") == bucket
                        and c["id"] not in duplicate_of and candidate_selectable(c)]
            maximum = [c for c in eligible if is_maximum(c)]
            chosen_ids = {c["id"] for c in chosen}
            if len(maximum) <= limit and not {c["id"] for c in maximum} <= chosen_ids:
                raise ValueError(f"Fonte máxima elegível omitida em {topic}/{bucket}.")
            if len(maximum) > limit and any(not is_maximum(c) for c in chosen):
                raise ValueError(f"Fonte comum ocupou vaga reservada por fontes máximas em {topic}/{bucket}.")
            expected = min(limit, len(eligible))
            if len(chosen) != expected:
                raise ValueError(f"{topic}/{bucket}: havia {len(eligible)} elegíveis e deveriam ser preenchidas {expected} vagas.")

    summary_items = []
    for selected in selections:
        candidate = candidates[selected["id"]]
        item = by_candidate_id.get(selected["id"])
        if item is None:
            raise ValueError("Texto original de item selecionado não foi localizado.")
        view = delivery_view(item, allow_limited=is_maximum(candidate))
        if not view["link_resolvido"]:
            raise ValueError("Link direto de item selecionado deixou de estar disponível.")
        link, body = view["link_final"], view["texto"]
        summary_items.append({"id": selected["id"], "topico": candidate["topico"],
            "bucket": evaluations[selected["id"]]["bucket"], "titulo": candidate["titulo"],
            "fonte": candidate["fonte"], "link": link, "texto": clean(body)[:ARTICLE_TEXT_LIMIT],
            "base_resumo": view["nivel"], "palavras_disponiveis": view["palavras"],
            "requer_leitura_url": view["nivel"] in ("insuficiente", "trecho_limitado"),
            "assunto": selected.get("assunto")})
    summary_request = {"schema": REQUEST_SCHEMA, "request_sha256": request["request_sha256"],
                       "items": summary_items}
    summary_request["summary_sha256"] = hashlib.sha256(json.dumps(
        summary_request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    save_compact_json(STATE_FILE, state)
    save_compact_json(SUMMARY_REQUEST_FILE, summary_request)
    print(json.dumps({"status": "ranking_validated", "summary_request": str(SUMMARY_REQUEST_FILE),
                      "selected": len(summary_items), "send_enabled": False}, ensure_ascii=False))


def _loaded_request():
    request = carregar_json(REQUEST_FILE, None)
    if not isinstance(request, dict) or not isinstance(request.get("candidates"), list):
        raise ValueError("Execute load-input antes de dividir ou juntar lotes.")
    return request


def _batch_manifest(request):
    manifest = carregar_json(BATCH_DIR / "manifest.json", None)
    if not isinstance(manifest, dict) or manifest.get("request_sha256") != request["request_sha256"]:
        raise ValueError("Lotes ausentes ou de outra coleta; execute split-batches.")
    return manifest


def split_batches(size=EVALUATION_BATCH_SIZE):
    """Divide os candidatos novos em arquivos autossuficientes para subagentes."""
    request = _loaded_request()
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    for stale in BATCH_DIR.glob("*.json"):
        stale.unlink()
    batches = []
    for topic in TOPIC_ORDER:
        pending = [c for c in request["candidates"]
                   if c.get("topico") == topic and c.get("precisa_avaliar")]
        for start in range(0, len(pending), size):
            name = f"{topic}_{start // size + 1:02d}"
            path, answer = BATCH_DIR / f"{name}.json", BATCH_DIR / f"{name}.resposta.json"
            # A leitura e o marcador de avaliação não pesam na decisão editorial.
            rows = [{k: v for k, v in c.items() if k not in ("leitura", "precisa_avaliar", "topico")}
                    for c in pending[start:start + size]]
            save_compact_json(path, {"lote": name, "topico": topic,
                "foco": request.get("focus", FOCO_SETORIAL)[topic], "regras": BATCH_RULES,
                "resposta": str(answer), "formato_resposta": {"evaluations": [
                    {"id": "...", "decisao": "fora_tema"},
                    {"id": "...", "decisao": "elegivel", "bucket": "BR", "prioridade": 80,
                     "fato": "id-curto-do-fato"}]},
                "candidates": rows})
            batches.append({"lote": name, "topico": topic, "quantidade": len(rows),
                            "arquivo": str(path), "resposta": str(answer)})
    manifest = {"request_sha256": request["request_sha256"], "lotes": batches}
    save_compact_json(BATCH_DIR / "manifest.json", manifest)
    print(json.dumps({"status": "batches_ready", "lotes": len(batches),
                      "candidatos": sum(b["quantidade"] for b in batches),
                      "manifesto": str(BATCH_DIR / "manifest.json")}, ensure_ascii=False))


def merge_batches():
    """Junta as respostas dos lotes, valida cada uma e prepara a rodada final."""
    request = _loaded_request()
    manifest = _batch_manifest(request)
    candidates = {c["id"]: c for c in request["candidates"]}
    evaluations, problems, batch_of = [], {}, {}
    for batch in manifest["lotes"]:
        expected = {c["id"] for c in carregar_json(Path(batch["arquivo"]), {}).get("candidates", [])}
        batch_of.update(dict.fromkeys(expected, batch["lote"]))
        answer = carregar_json(Path(batch["resposta"]), None)
        rows = answer.get("evaluations") if isinstance(answer, dict) else None
        if not isinstance(rows, list):
            problems[batch["lote"]] = "resposta ausente"
            continue
        ids = [r.get("id") for r in rows if isinstance(r, dict)]
        if len(ids) != len(rows) or len(set(ids)) != len(ids) or set(ids) != expected:
            problems[batch["lote"]] = "a resposta deve cobrir exatamente os candidatos do lote"
            continue
        try:
            evaluations += [{"id": r["id"], **valid_evaluation(r, candidates[r["id"]])} for r in rows]
        except ValueError as error:
            problems[batch["lote"]] = str(error)
    if problems:
        print(json.dumps({"status": "batches_incomplete", "refazer": problems}, ensure_ascii=False))
        raise ValueError(f"{len(problems)} lote(s) precisam ser refeitos: {', '.join(problems)}")

    # Rodada final: todas as fontes máximas e as melhores notas de cada geografia.
    decided = {row["id"]: row for row in evaluations}
    sent_by_topic = {}
    for fact in load_state().get("sent_facts", []):
        sent_by_topic.setdefault(fact.get("topico"), []).append(
            {"titulo": fact.get("titulo"), "fato": fact.get("fato"), "assunto": fact.get("assunto")})
    shortlist = {}
    for topic in TOPIC_ORDER:
        shortlist[topic] = {}
        for bucket in ("BR", "US"):
            rows = []
            for cid, candidate in candidates.items():
                evaluation = decided.get(cid) or candidate.get("avaliacao_cache") or {}
                if (candidate.get("topico") != topic or evaluation.get("decisao") != "elegivel"
                        or evaluation.get("bucket") != bucket or not candidate_selectable(candidate)):
                    continue
                # Mesma regra da V1: veículo do catálogo ou conteúdo com nota alta
                # vem antes de fonte comum, que só completa vagas.
                priority = (candidate.get("fonte_prioritaria") is True
                            or evaluation["prioridade"] >= LIMIAR_PRIORIDADE_CONTEUDO)
                rows.append({"id": cid, "titulo": candidate["titulo"], "fonte": candidate["fonte"],
                    "dominio": candidate.get("dominio"), "publicado_em": candidate.get("publicado_em"),
                    "prioridade": evaluation["prioridade"], "fato": evaluation["fato"],
                    "fonte_maxima": is_maximum(candidate), "prioritaria": priority,
                    "nivel": candidate["leitura"]["nivel"],
                    "trecho": candidate.get("trecho", "")[:SHORTLIST_EXCERPT]})
            rows.sort(key=lambda r: (not r["fonte_maxima"], not r["prioritaria"],
                                     -r["prioridade"], r["id"]))
            others = [r for r in rows if not r["fonte_maxima"]]
            # Avaliações de dias anteriores formam um grupo próprio.
            chosen = {r["id"] for r in rows if r["fonte_maxima"]}
            chosen.update(r["id"] for r in others[:SHORTLIST_PER_BUCKET])
            per_batch = Counter()
            for r in others:
                group = batch_of.get(r["id"], "cache")
                if per_batch[group] < SHORTLIST_PER_BATCH:
                    per_batch[group] += 1
                    chosen.add(r["id"])
            shortlist[topic]["ja_enviados"] = sent_by_topic.get(topic, [])
            shortlist[topic][bucket] = {"vagas": VAGAS[topic][bucket], "elegiveis_total": len(rows),
                                        "finalistas": [r for r in rows if r["id"] in chosen]}
    save_compact_json(RANKING_RESPONSE_FILE, {"request_sha256": request["request_sha256"],
        "evaluations": evaluations, "selections": [], "duplicates": {}})
    save_compact_json(SHORTLIST_FILE, {"request_sha256": request["request_sha256"],
                                       "limits": VAGAS, "topicos": shortlist})
    print(json.dumps({"status": "batches_merged", "avaliacoes": len(evaluations),
        "elegiveis": {t: {b: shortlist[t][b]["elegiveis_total"] for b in ("BR", "US")}
                      for t in TOPIC_ORDER},
        "finalistas": str(SHORTLIST_FILE)}, ensure_ascii=False))


def _subject_key(text):
    return " ".join(re.sub(r"[^\w]+", " ", str(text or "").casefold()).split())


def check_subject_diversity(selections):
    """Um assunto por tópico: prioridade de regulação não pode lotar as vagas.

    Coberturas diferentes do mesmo tema (ex.: sanção do REDATA, Moody's sobre o
    REDATA, empresa comentando o REDATA) são fatos distintos para a deduplicação,
    mas repetem o assunto. Uma segunda matéria do assunto exige justificativa.
    """
    seen = set()
    for selected in selections:
        subject = _subject_key(selected.get("assunto"))
        if not subject:
            raise ValueError(f"Seleção {selected.get('id')} sem assunto.")
        key = (selected.get("topico"), subject)
        if key in seen and not str(selected.get("repeticao_justificada", "")).strip():
            raise ValueError(f"Assunto repetido em {key[0]}: {selected.get('assunto')}. "
                             "Escolha outro assunto ou justifique o desdobramento novo.")
        seen.add(key)


def select(selection_path):
    """Grava seleção e duplicidades na resposta montada e executa validate-ranking."""
    selection = carregar_json(Path(selection_path), None)
    response = carregar_json(RANKING_RESPONSE_FILE, None)
    if not isinstance(selection, dict) or not isinstance(response, dict):
        raise ValueError("Execute merge-batches e grave o arquivo de seleção antes.")
    if not isinstance(selection.get("selections"), list):
        raise ValueError("selecao.json precisa de uma lista selections.")
    check_subject_diversity(selection["selections"])
    response["selections"] = selection.get("selections")
    response["duplicates"] = selection.get("duplicates", {})
    save_compact_json(RANKING_RESPONSE_FILE, response)
    validate_ranking()


def validate_summary_text(text, title):
    text = str(text or "").strip()
    if not text or len(text) > 1800:
        raise ValueError("Resumo vazio ou excessivamente longo.")
    if re.search(r"<[^>]+>", text):
        raise ValueError("Resumo contém HTML.")
    if clean(text).casefold() == clean(title).casefold():
        raise ValueError("Resumo apenas repete a manchete.")
    return text


def finalize(response_path=None):
    response_path = Path(response_path) if response_path else SUMMARY_RESPONSE_FILE
    request = carregar_json(REQUEST_FILE, None)
    summary_request = carregar_json(SUMMARY_REQUEST_FILE, None)
    response = carregar_json(response_path, None)
    if not request or not summary_request or not response:
        raise ValueError("Ranking deve ser validado e os resumos gravados antes de finalizar.")
    if response.get("summary_sha256") != summary_request["summary_sha256"]:
        raise ValueError("Os resumos pertencem a outra seleção; nada foi finalizado.")
    rows = response.get("summaries")
    if not isinstance(rows, list) or len(rows) != len(summary_request["items"]):
        raise ValueError("Quantidade incorreta de resumos.")
    by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    expected = {i["id"] for i in summary_request["items"]}
    if set(by_id) != expected:
        raise ValueError("Resumos ausentes, duplicados ou desconhecidos.")

    selection = {t: {"BR": [], "US": []} for t in TOPIC_ORDER}
    for item in summary_request["items"]:
        summary_row = by_id[item["id"]]
        reading_base = item["base_resumo"]
        if item.get("requer_leitura_url"):
            url_reading = summary_row.get("leitura_url")
            if url_reading not in ("confirmada", "indisponivel"):
                raise ValueError("Finalista sem confirmação da tentativa de leitura da URL.")
            reading_base = ("artigo_lido_pelo_modelo" if url_reading == "confirmada"
                            else "trecho_limitado_final")
        selection[item["topico"]][item["bucket"]].append({
            "titulo": item["titulo"], "fonte": item["fonte"], "link": item["link"],
            "base_resumo": reading_base,
            "resumo_final": validate_summary_text(summary_row.get("resumo"), item["titulo"])})
    now_brt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
    period = "manhã" if now_brt.hour < 14 else "tarde"
    moment = now_brt.strftime(f"%d/%m/%Y · {period}")
    PREVIEW_FILE.write_text(montar_html(selection, moment), encoding="utf-8")
    selected_count = sum(len(selection[t][b]) for t in TOPIC_ORDER for b in ("BR", "US"))
    pending_links = sum(google_pendente(item) for topic in TOPIC_ORDER
                        for bucket in ("BR", "US") for item in selection[topic][bucket])
    selected_bases = Counter(item["base_resumo"] for topic in TOPIC_ORDER
                             for bucket in ("BR", "US") for item in selection[topic][bucket])
    report = {"status": "pilot_ready" if selected_count else "pilot_empty",
        "send_enabled": False,
        "request_sha256": request["request_sha256"], "generated_at": time.time(),
        "counts": {t: {b: len(selection[t][b]) for b in ("BR", "US")} for t in TOPIC_ORDER},
        "google_links_pending": pending_links,
        "delivery_links_ready": pending_links == 0,
        "reading": {"candidates": request.get("reading", {}),
                    "selected": dict(selected_bases)},
        "needs_evaluation": sum(c["precisa_avaliar"] for c in request["candidates"]),
        "cached": sum(not c["precisa_avaliar"] for c in request["candidates"]),
        "truncated": request.get("truncated", {}),
        "collection": request.get("collection", {}), "preview": str(PREVIEW_FILE),
        "subject": f"Panorama Data Centers, Baterias & Carbono — {now_brt.strftime('%d/%m')} ({period})",
        "selected": [{"id": item["id"], "topico": item["topico"], "link": item["link"],
                      "titulo": item["titulo"], "assunto": item.get("assunto")}
                     for item in summary_request["items"]],
        "note": "A Routine não envia e-mail; o envio ocorre no GitHub Actions."}
    salvar_json(REPORT_FILE, report)
    print(json.dumps(report, ensure_ascii=False))


BRT = datetime.timezone(datetime.timedelta(hours=-3))


def _brt_date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, BRT).date()


def delivered_today(state, now=None):
    """Um e-mail por dia: enviado hoje, ou com entrega incerta iniciada hoje."""
    today = _brt_date(now or time.time())
    stamps = [state.get("last_sent_at"), (state.get("delivery_pending") or {}).get("since")]
    return any(isinstance(t, (int, float)) and _brt_date(t) == today for t in stamps)


def _send_via_brevo(subject, html):
    recipients = [{"email": e.strip()} for e in re.split(r"[,;]", os.environ.get("EMAIL_DESTINO", ""))
                  if e.strip()]
    if not (os.environ.get("BREVO_API_KEY") and os.environ.get("EMAIL_REMETENTE") and recipients):
        raise RuntimeError("Credenciais de envio ausentes; nada foi enviado.")
    response = requests.post("https://api.brevo.com/v3/smtp/email", timeout=30, headers={
        "api-key": os.environ["BREVO_API_KEY"], "content-type": "application/json",
        "accept": "application/json"}, json={
        "sender": {"name": "Panorama DC, Baterias & Carbono", "email": os.environ["EMAIL_REMETENTE"]},
        "to": recipients, "subject": subject, "htmlContent": html})
    if 400 <= response.status_code < 500:
        return "rejeitado", f"HTTP {response.status_code}"
    if response.status_code != 201 or not response.json().get("messageId"):
        return "incerto", f"HTTP {response.status_code}"
    return "enviado", response.json()["messageId"]


def send():
    """Envia a edição finalizada e marca as matérias como enviadas no estado.

    Roda somente no GitHub Actions, com os secrets da Brevo. Cada coleta é enviada
    no máximo uma vez: uma entrega incerta bloqueia a repetição daquela edição.
    """
    report = carregar_json(REPORT_FILE, None)
    if not isinstance(report, dict) or report.get("status") != "pilot_ready":
        print("Nenhuma edição pronta para envio.")
        return
    if not report.get("delivery_links_ready") or not report.get("selected"):
        raise RuntimeError("Edição com links pendentes ou sem seleção; nada foi enviado.")
    state = load_state()
    edition = report["request_sha256"]
    if edition in (state.get("last_sent_request"), (state.get("delivery_pending") or {}).get("request")):
        print("Esta edição já foi enviada ou tem entrega incerta; nada foi reenviado.")
        return
    # A Routine tem uma segunda tentativa no dia; se as duas terminarem, só a
    # primeira edição é enviada.
    if delivered_today(state):
        print("O e-mail de hoje já foi enviado; esta edição não será enviada.")
        return
    by_candidate_id = {candidate_id(topic, item): item for item in state.get("items", {}).values()
                       for topic in item.get("topicos", [])}
    aliases = set()
    for selected in report["selected"]:
        item = by_candidate_id.get(selected["id"])
        aliases |= identidades(item) if item else set()
        aliases.add(canonica(selected["link"]))
    aliases.discard("")
    state["delivery_pending"] = {"request": edition, "subject": report["subject"],
                                 "aliases": sorted(aliases), "since": time.time()}
    save_compact_json(STATE_FILE, state)
    outcome, detail = _send_via_brevo(report["subject"], PREVIEW_FILE.read_text(encoding="utf-8"))
    if outcome == "incerto":
        raise RuntimeError(f"Entrega incerta ({detail}); edição bloqueada para não duplicar.")
    state.pop("delivery_pending", None)
    if outcome == "rejeitado":
        save_compact_json(STATE_FILE, state)
        raise RuntimeError(f"Brevo rejeitou o e-mail ({detail}); nada foi marcado como enviado.")
    state["sent"] = sorted(set(state.get("sent", [])) | aliases)
    now = time.time()
    facts = [f for f in state.get("sent_facts", [])
             if now - f.get("sent_at", 0) <= SENT_FACTS_DAYS * 86400]
    for selected in report["selected"]:
        evaluation = state.get("evaluations", {}).get(selected["id"], {}).get("avaliacao", {})
        facts.append({"topico": selected["topico"], "titulo": selected.get("titulo"),
                      "fato": evaluation.get("fato"), "assunto": selected.get("assunto"),
                      "sent_at": now})
    state["sent_facts"] = facts
    state["last_sent_request"] = edition
    state["last_sent_at"] = now
    save_compact_json(STATE_FILE, state)
    report["email"] = {"status": "enviado", "message_id": detail, "sent_at": time.time()}
    salvar_json(REPORT_FILE, report)
    print(json.dumps({"status": "email_sent", "subject": report["subject"],
                      "items": len(report["selected"])}, ensure_ascii=False))


def _git(*args, capture=False):
    # UTF-8 explícito: no Windows o padrão cp1252 quebra a leitura do JSON.
    result = subprocess.run(["git", *args], cwd=ROOT, check=True, text=True,
                            encoding="utf-8", capture_output=capture)
    return result.stdout.strip() if capture else None


def sent_today():
    """Primeiro passo da Routine: evita refazer o trabalho se o e-mail já saiu."""
    _git("fetch", "-q", "origin", BRANCH)
    state = json.loads(_git("show", f"origin/{BRANCH}:{STATE_FILE.name}", capture=True))
    done = delivered_today(state)
    print(json.dumps({"status": "already_sent" if done else "not_sent",
                      "sent_today": done}, ensure_ascii=False))


def request_input():
    """Pede ao GitHub uma coleta nova, sem depender do agendador do GitHub.

    O agendador do Actions atrasa horas; um push dispara o workflow na hora.
    A Routine só consegue gravar branches claude/, então o pedido é uma branch
    claude/v2-coleta-* com um arquivo de marcação. O workflow a apaga no fim.
    """
    WORK_DIR.mkdir(exist_ok=True)
    since = int(time.time())
    branch = f"claude/v2-coleta-{since}"
    current = _git("rev-parse", "--abbrev-ref", "HEAD", capture=True)
    _git("checkout", "-q", "-b", branch)
    try:
        TRIGGER_FILE.write_text(f"Coleta solicitada pela Routine em {since}\n", encoding="utf-8")
        _git("add", "--", TRIGGER_FILE.name)
        _git("commit", "-q", "-m", "Solicita coleta V2")
        _git("push", "-q", "origin", branch)
    finally:
        _git("checkout", "-q", current)
        _git("branch", "-q", "-D", branch)
    salvar_json(COLLECTION_REQUEST_FILE, {"since": since, "branch": branch})
    print(json.dumps({"status": "collection_requested", "branch": branch, "since": since},
                     ensure_ascii=False))


def wait_input(max_minutes=1.5):
    """Espera a coleta pedida chegar à branch V2 e atualiza a cópia local.

    Sai com código 3 se o prazo desta chamada acabar: cada chamada dura no
    máximo 1,5 min, abaixo do limite padrão de 2 min de um comando na Routine;
    basta executar de novo.
    """
    request = carregar_json(COLLECTION_REQUEST_FILE, None)
    if not isinstance(request, dict):
        raise ValueError("Execute request-input antes de esperar a coleta.")
    deadline = time.time() + max_minutes * 60
    while True:
        _git("fetch", "-q", "origin", BRANCH)
        try:
            snapshot = json.loads(_git("show", f"origin/{BRANCH}:{INPUT_FILE.name}", capture=True))
        except (subprocess.CalledProcessError, ValueError):
            snapshot = {}
        if snapshot.get("trigger") == request["branch"]:
            _git("merge", "-q", "--ff-only", f"origin/{BRANCH}")
            print(json.dumps({"status": "input_ready", "waited_seconds":
                              round(time.time() - request["since"])}, ensure_ascii=False))
            return
        if time.time() >= deadline:
            print(json.dumps({"status": "still_waiting", "since": request["since"]}, ensure_ascii=False))
            raise SystemExit(3)
        time.sleep(WAIT_POLL_SECONDS)


def status():
    print(json.dumps({"branch_expected": "v2-claude-routines", "state": STATE_FILE.exists(),
        "preflight": carregar_json(PREFLIGHT_FILE, None),
        "input": INPUT_FILE.exists(),
        "ranking_request": REQUEST_FILE.exists(), "ranking_response": RANKING_RESPONSE_FILE.exists(),
        "summary_request": SUMMARY_REQUEST_FILE.exists(), "summary_response": SUMMARY_RESPONSE_FILE.exists(),
        "preview": PREVIEW_FILE.exists(), "report": carregar_json(REPORT_FILE, None),
        "send_enabled": False}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Piloto sem API do digest em Claude Code Routines")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    prep = sub.add_parser("prepare")
    prep.add_argument("--max-new-per-topic", type=int, default=0,
                      help="0 processa todos os candidatos; valor positivo limita por tópico")
    sub.add_parser("sent-today")
    sub.add_parser("request-input")
    wait = sub.add_parser("wait-input")
    wait.add_argument("--max-minutes", type=float, default=1.5)
    load = sub.add_parser("load-input")
    load.add_argument("--max-age-hours", type=float, default=6,
                      help="idade máxima aceita para a coleta preparada pelo GitHub Actions")
    split = sub.add_parser("split-batches")
    split.add_argument("--size", type=int, default=EVALUATION_BATCH_SIZE)
    sub.add_parser("merge-batches")
    choose = sub.add_parser("select")
    choose.add_argument("--file", default=str(WORK_DIR / "selecao.json"))
    rank = sub.add_parser("validate-ranking")
    rank.add_argument("--response", default=str(RANKING_RESPONSE_FILE))
    summaries = sub.add_parser("finalize")
    summaries.add_argument("--response", default=str(SUMMARY_RESPONSE_FILE))
    sub.add_parser("send")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "preflight":
        preflight()
    elif args.command == "prepare":
        prepare(args.max_new_per_topic)
    elif args.command == "sent-today":
        sent_today()
    elif args.command == "request-input":
        request_input()
    elif args.command == "wait-input":
        wait_input(args.max_minutes)
    elif args.command == "load-input":
        load_input(args.max_age_hours)
    elif args.command == "split-batches":
        split_batches(args.size)
    elif args.command == "merge-batches":
        merge_batches()
    elif args.command == "select":
        select(args.file)
    elif args.command == "validate-ranking":
        validate_ranking(Path(args.response))
    elif args.command == "finalize":
        finalize(Path(args.response))
    elif args.command == "send":
        send()
    else:
        status()


if __name__ == "__main__":
    main()
