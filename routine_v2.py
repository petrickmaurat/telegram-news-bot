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
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

import common
from common import TOPICOS, coletar_itens_novos, resolver_link_google_news, salvar_cache_google
from digest_email import FOCO_SETORIAL, VAGAS, montar_html
from email_articles import enriquecer_fila
from email_language import idioma_permitido
from email_policy import IDADE_MAXIMA, recente
from email_sources import configuracao_email, fonte_maxima, fonte_prioritaria
from email_topic import verificar_tema
from reliability import canonica, carregar_json, identidades, salvar_json


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "routine_v2_state.json"
GOOGLE_CACHE_FILE = ROOT / "routine_v2_google_cache.json"
WORK_DIR = ROOT / "routine_v2_work"
REQUEST_FILE = WORK_DIR / "ranking_request.json"
RANKING_RESPONSE_FILE = WORK_DIR / "ranking_response.json"
SUMMARY_REQUEST_FILE = WORK_DIR / "summary_request.json"
SUMMARY_RESPONSE_FILE = WORK_DIR / "summary_response.json"
REPORT_FILE = ROOT / "routine_v2_report.json"
PREVIEW_FILE = ROOT / "routine_v2_preview.html"
POLICY_VERSION = 1
DECISIONS = {"elegivel", "fora_tema", "sem_fato_novo", "fonte_duvidosa"}
TOPIC_ORDER = ("data_center", "baterias", "carbono")


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


def resolve_candidates(items):
    for item in items:
        if urlsplit(canonica(item.get("link", ""))).hostname != "news.google.com":
            continue
        old_ids = identidades(item)
        resolved = resolver_link_google_news(item["link"])
        if canonica(resolved) and urlsplit(canonica(resolved)).hostname != "news.google.com":
            item["link"] = resolved
            item["aliases"] = sorted(old_ids | identidades(item))


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


def prepare(max_new_per_topic=60):
    WORK_DIR.mkdir(exist_ok=True)
    activate_google_cache()
    state = load_state()
    now = time.time()
    state["items"] = {key: item for key, item in state.get("items", {}).items()
                      if recente(item, now) or (item.get("publicado_em") is None
                      and now - item.get("_v2_seen_at", now) <= 7 * 86400)}
    sent = {canonica(x) for x in state.get("sent", [])}
    sources = []
    collected = coletar_itens_novos(sent, resolver=False,
        configuracao=configuracao_email(TOPICOS), relatorio_fontes=sources)
    for item in collected:
        key = item_key(item)
        state["items"][key] = merge_item(state["items"].get(key), item)

    # Enriquece uma cópia da fila; não chama qualquer modelo de IA.
    queue = {key: {"item": item, "status": "pendente"}
             for key, item in state["items"].items()}
    enriquecer_fila(queue, resolver_link_google_news, time.time())
    items = [r["item"] for r in queue.values()]
    resolve_candidates(items)
    items = dedupe_same_article(items)
    state["items"] = {item_key(item): item for item in items}
    salvar_cache_google()

    now = time.time()
    candidates = []
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
                "link": canonica(item["link"]), "trecho": clean(item.get("resumo", ""))[:600],
                "publicado_em": item.get("publicado_em"), "fonte_maxima": fonte_maxima(item),
                "fonte_prioritaria": bool(fonte_prioritaria(item)), "assinatura": signature,
                "precisa_avaliar": not valid_cache,
                "avaliacao_cache": cached.get("avaliacao") if valid_cache else None,
            }
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

    request = {
        "schema": 1, "policy_version": POLICY_VERSION,
        "run_id": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": now, "pilot": True, "send_enabled": False,
        "limits": VAGAS, "topic_order": list(TOPIC_ORDER), "focus": FOCO_SETORIAL,
        "batch_size": 30, "batch_winners_per_bucket": 10,
        "candidates": candidates,
        "local_rejections": [{"topico": topic, "resultado": reason, "quantidade": count}
                             for (topic, reason), count in sorted(rejected_locally.items())],
        "truncated": truncated, "source_queries": len(sources),
    }
    request["request_sha256"] = hashlib.sha256(json.dumps(
        request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    live_evaluation_ids = {candidate_id(topic, item) for item in items
                           for topic in item.get("topicos", [])}
    state["evaluations"] = {key: value for key, value in state.get("evaluations", {}).items()
                            if key in live_evaluation_ids}
    salvar_json(STATE_FILE, state)
    salvar_json(REQUEST_FILE, request)
    for stale in (RANKING_RESPONSE_FILE, SUMMARY_REQUEST_FILE, SUMMARY_RESPONSE_FILE, PREVIEW_FILE):
        if stale.exists():
            stale.unlink()
    print(json.dumps({"status": "prepared", "request": str(REQUEST_FILE),
        "candidates": len(candidates),
        "needs_evaluation": sum(c["precisa_avaliar"] for c in candidates),
        "cached": sum(not c["precisa_avaliar"] for c in candidates),
        "truncated": truncated, "send_enabled": False}, ensure_ascii=False))


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
    evaluations = {}
    for cid, candidate in candidates.items():
        if candidate["precisa_avaliar"]:
            evaluation = valid_evaluation(supplied[cid], candidate)
            state.setdefault("evaluations", {})[cid] = {
                "assinatura": candidate["assinatura"], "avaliacao": evaluation}
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
                        and c["id"] not in duplicate_of]
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
    by_item_key = {item_key(item): item for item in state["items"].values()}
    for selected in selections:
        candidate = candidates[selected["id"]]
        item = by_item_key.get(candidate["link"])
        if item is None:
            raise ValueError("Texto original de item selecionado não foi localizado.")
        body = item.get("artigo", {}).get("texto") or clean(item.get("resumo", ""))
        summary_items.append({"id": selected["id"], "topico": candidate["topico"],
            "bucket": evaluations[selected["id"]]["bucket"], "titulo": candidate["titulo"],
            "fonte": candidate["fonte"], "link": candidate["link"], "texto": clean(body)[:3000]})
    summary_request = {"schema": 1, "request_sha256": request["request_sha256"],
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
            "resumo_final": validate_summary_text(by_id[item["id"]].get("resumo"), item["titulo"])})
    moment = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).strftime(
        "%d/%m/%Y · piloto Claude Routine")
    PREVIEW_FILE.write_text(montar_html(selection, moment), encoding="utf-8")
    report = {"status": "pilot_ready", "send_enabled": False,
        "request_sha256": request["request_sha256"], "generated_at": time.time(),
        "counts": {t: {b: len(selection[t][b]) for b in ("BR", "US")} for t in TOPIC_ORDER},
        "needs_evaluation": sum(c["precisa_avaliar"] for c in request["candidates"]),
        "cached": sum(not c["precisa_avaliar"] for c in request["candidates"]),
        "truncated": request.get("truncated", {}), "preview": str(PREVIEW_FILE),
        "note": "Piloto: nenhum e-mail foi enviado e o histórico da V1 não foi alterado."}
    salvar_json(REPORT_FILE, report)
    print(json.dumps(report, ensure_ascii=False))


def status():
    print(json.dumps({"branch_expected": "v2-claude-routines", "state": STATE_FILE.exists(),
        "ranking_request": REQUEST_FILE.exists(), "ranking_response": RANKING_RESPONSE_FILE.exists(),
        "summary_request": SUMMARY_REQUEST_FILE.exists(), "summary_response": SUMMARY_RESPONSE_FILE.exists(),
        "preview": PREVIEW_FILE.exists(), "report": carregar_json(REPORT_FILE, None),
        "send_enabled": False}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Piloto sem API do digest em Claude Code Routines")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--max-new-per-topic", type=int, default=60,
                      help="0 remove o limite; mantenha 60 no primeiro piloto")
    rank = sub.add_parser("validate-ranking")
    rank.add_argument("--response", default=str(RANKING_RESPONSE_FILE))
    summaries = sub.add_parser("finalize")
    summaries.add_argument("--response", default=str(SUMMARY_RESPONSE_FILE))
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.max_new_per_topic)
    elif args.command == "validate-ranking":
        validate_ranking(Path(args.response))
    elif args.command == "finalize":
        finalize(Path(args.response))
    else:
        status()


if __name__ == "__main__":
    main()
