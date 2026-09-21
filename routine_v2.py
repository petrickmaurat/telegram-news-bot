"""Piloto do digest executado por uma Claude Code Routine, sem API de IA.

O script cuida de coleta, estado e validação. A Routine só toma decisões
editoriais e grava JSON nos formatos documentados em ROUTINE_V2_INSTRUCTIONS.md.
Nenhum comando deste piloto envia e-mail ou altera o histórico da V1.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path

import common
import requests
import trafilatura
from common import TOPICOS, coletar_itens_novos, resolver_link_google_news, salvar_cache_google
from digest_email import FOCO_SETORIAL, VAGAS, montar_html
from email_articles import enriquecer_fila
from email_language import idioma_permitido
from email_policy import IDADE_MAXIMA, google_pendente, recente
from email_sources import configuracao_email, fonte_maxima, fonte_prioritaria
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
POLICY_VERSION = 1
REQUEST_SCHEMA = 2
DECISIONS = {"elegivel", "fora_tema", "sem_fato_novo", "fonte_duvidosa"}
TOPIC_ORDER = ("data_center", "baterias", "carbono")
MIN_ARTICLE_WORDS = 80
MIN_EXCERPT_WORDS = 40
MIN_LIMITED_WORDS = 10
ARTICLE_TEXT_LIMIT = 6000
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
            "selecionavel": link_ready and level != "insuficiente", "texto": text}


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
    """Só une a mesma URL canônica. Manchetes parecidas continuam separadas."""
    merged = {}
    for item in items:
        key = item_key(item)
        merged[key] = merge_item(merged.get(key), item)
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
             REPORT_FILE, PREVIEW_FILE]
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
                      if recente(item, now) or (item.get("publicado_em") is None
                      and now - item.get("_v2_seen_at", now) <= 7 * 86400)}
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
            candidate = {
                "id": cid, "topico": topic, "titulo": item["titulo"], "fonte": item.get("fonte", ""),
                "trecho": clean(item.get("resumo", ""))[:600],
                "publicado_em": item.get("publicado_em"), "fonte_maxima": fonte_maxima(item),
                "fonte_prioritaria": bool(fonte_prioritaria(item)),
                "precisa_avaliar": not valid_cache,
            }
            candidate_items[cid] = item
            if valid_cache:
                candidate["avaliacao_cache"] = cached["avaliacao"]
            # O link opaco do Google Noticias pode ter centenas de caracteres
            # e nao acrescenta informacao editorial. A V1 tambem o omite da IA;
            # a V2 o resolve somente se a materia chegar a selecao final.
            if not google_pendente(item):
                candidate["link"] = canonica(item["link"])
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
        candidate["fonte_maxima"] = fonte_maxima(delivery_item)
        candidate["fonte_prioritaria"] = bool(fonte_prioritaria(delivery_item))
        view = delivery_view(item, allow_limited=candidate["fonte_maxima"])
        candidate["trecho"] = view["texto"][:600]
        candidate["leitura"] = {key: view[key] for key in
                                ("nivel", "palavras", "link_resolvido", "selecionavel")}
        if view["link_resolvido"]:
            candidate["link"] = view["link_final"]
        else:
            candidate.pop("link", None)
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
    }
    request["request_sha256"] = request_sha256(request)
    live_evaluation_ids = {candidate_id(topic, item) for item in items
                           for topic in item.get("topicos", [])}
    state["evaluations"] = {key: value for key, value in state.get("evaluations", {}).items()
                            if key in live_evaluation_ids}
    salvar_json(STATE_FILE, state)
    salvar_json(REQUEST_FILE, request)
    salvar_json(INPUT_FILE, request)
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
        raise ValueError(f"Entrada V2 fora da janela de {max_age_hours:g} hora(s).")
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
                or type(reading.get("selecionavel")) is not bool
                or type(reading.get("link_resolvido")) is not bool):
            raise ValueError("Entrada V2 contém candidato sem diagnóstico de leitura.")

    # Feeds acessiveis nao garantem que os links finais estejam legiveis. A
    # Routine so deve consumir a franquia do Claude quando houver ao menos
    # material selecionavel para preencher todas as vagas de cada tema.
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
        raise ValueError("Entrada V2 sem cobertura legível suficiente: "
                         + json.dumps(missing, ensure_ascii=False, sort_keys=True))

    salvar_json(REQUEST_FILE, request)
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
    reading = candidate.get("leitura", {})
    return (reading.get("selecionavel") is True
            and reading.get("link_resolvido") is True
            and reading.get("nivel") in
                ("artigo_completo", "trecho_disponivel", "trecho_limitado"))


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
        if candidates[child]["fonte_maxima"] and not candidates[parent]["fonte_maxima"]:
            raise ValueError("Uma fonte máxima não pode ser descartada em favor de fonte comum.")

    groups = {t: {"BR": [], "US": []} for t in TOPIC_ORDER}
    for selected in selections:
        cid = selected["id"]
        candidate, evaluation = candidates[cid], evaluations[cid]
        if not evaluation or evaluation.get("decisao") != "elegivel":
            raise ValueError("Um item selecionado não foi classificado como elegível.")
        if not candidate_selectable(candidate):
            raise ValueError("Um item selecionado nao tem link e conteudo suficientes para o resumo.")
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
            maximum = [c for c in eligible if c["fonte_maxima"]]
            chosen_ids = {c["id"] for c in chosen}
            if len(maximum) <= limit and not {c["id"] for c in maximum} <= chosen_ids:
                raise ValueError(f"Fonte máxima elegível omitida em {topic}/{bucket}.")
            if len(maximum) > limit and any(not c["fonte_maxima"] for c in chosen):
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
        view = delivery_view(item, allow_limited=candidate["fonte_maxima"])
        if not view["selecionavel"]:
            raise ValueError("Material de item selecionado deixou de ser suficiente.")
        link, body = view["link_final"], view["texto"]
        summary_items.append({"id": selected["id"], "topico": candidate["topico"],
            "bucket": evaluations[selected["id"]]["bucket"], "titulo": candidate["titulo"],
            "fonte": candidate["fonte"], "link": link, "texto": clean(body)[:ARTICLE_TEXT_LIMIT],
            "base_resumo": view["nivel"], "palavras_disponiveis": view["palavras"]})
    summary_request = {"schema": REQUEST_SCHEMA, "request_sha256": request["request_sha256"],
                       "items": summary_items}
    summary_request["summary_sha256"] = hashlib.sha256(json.dumps(
        summary_request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    salvar_json(STATE_FILE, state)
    salvar_json(SUMMARY_REQUEST_FILE, summary_request)
    print(json.dumps({"status": "ranking_validated", "summary_request": str(SUMMARY_REQUEST_FILE),
                      "selected": len(summary_items), "send_enabled": False}, ensure_ascii=False))


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
        selection[item["topico"]][item["bucket"]].append({
            "titulo": item["titulo"], "fonte": item["fonte"], "link": item["link"],
            "base_resumo": item["base_resumo"],
            "resumo_final": validate_summary_text(by_id[item["id"]].get("resumo"), item["titulo"])})
    moment = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).strftime(
        "%d/%m/%Y · piloto Claude Routine")
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
        "note": "Piloto: nenhum e-mail foi enviado e o histórico da V1 não foi alterado."}
    salvar_json(REPORT_FILE, report)
    print(json.dumps(report, ensure_ascii=False))


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
    load = sub.add_parser("load-input")
    load.add_argument("--max-age-hours", type=float, default=6,
                      help="idade máxima aceita para a coleta preparada pelo GitHub Actions")
    rank = sub.add_parser("validate-ranking")
    rank.add_argument("--response", default=str(RANKING_RESPONSE_FILE))
    summaries = sub.add_parser("finalize")
    summaries.add_argument("--response", default=str(SUMMARY_RESPONSE_FILE))
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "preflight":
        preflight()
    elif args.command == "prepare":
        prepare(args.max_new_per_topic)
    elif args.command == "load-input":
        load_input(args.max_age_hours)
    elif args.command == "validate-ranking":
        validate_ranking(Path(args.response))
    elif args.command == "finalize":
        finalize(Path(args.response))
    else:
        status()


if __name__ == "__main__":
    main()
