import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import routine_v2 as v2
from reliability import carregar_json, salvar_json


def news(number=1, source="Reuters", link=None):
    link = link or f"https://reuters.com/article/v2-{number}"
    return {"titulo": f"Novo data center recebe autorização para conexão elétrica {number}",
            "fonte": source, "link": link, "aliases": [link],
            "resumo": "O projeto de data center recebeu autorização e divulgou investimento e capacidade.",
            "topico": "data_center", "topicos": ["data_center"], "origem": "BR",
            "publicado_em": time.time(), "feeds_origem": ["feed"]}


class RoutineV2Tests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        values = {"STATE_FILE": root / "state.json", "WORK_DIR": root / "work",
                  "GOOGLE_CACHE_FILE": root / "v2_google.json",
                  "INPUT_FILE": root / "input.json",
                  "REQUEST_FILE": root / "work" / "ranking_request.json",
                  "RANKING_RESPONSE_FILE": root / "work" / "ranking_response.json",
                  "SUMMARY_REQUEST_FILE": root / "work" / "summary_request.json",
                  "SUMMARY_RESPONSE_FILE": root / "work" / "summary_response.json",
                  "REPORT_FILE": root / "report.json", "PREVIEW_FILE": root / "preview.html",
                  "PREFLIGHT_FILE": root / "preflight.json"}
        for name, value in values.items():
            p = patch.object(v2, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("_CACHE_GOOGLE_FILE", str(root / "v1_google.json")),
                            ("_cache_google", {})):
            p = patch.object(v2.common, name, value)
            p.start()
            self.addCleanup(p.stop)
        v2.WORK_DIR.mkdir()

    def base_state(self, items):
        return {"version": 1, "items": {v2.item_key(i): i for i in items},
                "evaluations": {}, "sent": []}

    def test_prepare_is_capped_and_does_not_call_ai_or_send(self):
        items = [news(i) for i in range(3)]
        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=items), \
             patch.object(v2, "enriquecer_fila"), patch.object(v2, "resolve_candidates"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=2)
        request = carregar_json(v2.REQUEST_FILE, {})
        self.assertEqual(request, carregar_json(v2.INPUT_FILE, {}))
        self.assertFalse(request["send_enabled"])
        self.assertEqual(len(request["candidates"]), 2)
        self.assertEqual(request["truncated"]["data_center"], {
            "novos_incluidos": 2, "novos_aptos": 3, "elegiveis_em_cache": 0})

    def test_prepare_uses_more_feed_workers_only_in_v2(self):
        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=[]) as collect, \
             patch.object(v2, "enriquecer_fila"), patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        self.assertEqual(collect.call_args.kwargs["feed_workers"], 8)

    def test_preflight_requires_google_and_direct_feed(self):
        responses = [SimpleNamespace(status_code=200, content=b"rss", ok=True),
                     SimpleNamespace(status_code=403, content=b"", ok=False),
                     SimpleNamespace(status_code=403, content=b"", ok=False)]
        with patch.object(v2.requests, "get", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "Preflight"):
                v2.preflight()
        report = carregar_json(v2.PREFLIGHT_FILE, {})
        self.assertEqual(report["status"], "network_failed")

    def test_preflight_accepts_one_working_direct_feed(self):
        responses = [SimpleNamespace(status_code=200, content=b"rss", ok=True),
                     SimpleNamespace(status_code=403, content=b"", ok=False),
                     SimpleNamespace(status_code=200, content=b"rss", ok=True)]
        with patch.object(v2.requests, "get", side_effect=responses):
            v2.preflight()
        self.assertEqual(carregar_json(v2.PREFLIGHT_FILE, {})["status"], "network_ready")

    def test_prepare_aborts_when_collection_is_blocked(self):
        def blocked(*args, **kwargs):
            kwargs["relatorio_fontes"].extend([
                {"fonte": "Google", "resultado": "falha", "itens_rss": 0},
                {"fonte": "Direto", "resultado": "falha", "itens_rss": 0},
            ])
            return []

        salvar_json(v2.INPUT_FILE, {"old": True})
        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", side_effect=blocked):
            with self.assertRaisesRegex(RuntimeError, "Coleta indisponível"):
                v2.prepare(max_new_per_topic=0)
        report = carregar_json(v2.REPORT_FILE, {})
        self.assertEqual(report["status"], "collection_failed")
        self.assertFalse(v2.REQUEST_FILE.exists())
        self.assertFalse(v2.INPUT_FILE.exists())

    def test_zero_cap_processes_all_candidates(self):
        items = [news(i) for i in range(35)]
        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=items), \
             patch.object(v2, "enriquecer_fila"), patch.object(v2, "resolve_candidates"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        request = carregar_json(v2.REQUEST_FILE, {})
        self.assertEqual(len(request["candidates"]), 35)
        self.assertEqual(request["truncated"], {})

    def test_google_cache_is_redirected_to_v2_file(self):
        original = v2.common._CACHE_GOOGLE_FILE
        v2.activate_google_cache()
        self.assertEqual(v2.common._CACHE_GOOGLE_FILE, str(v2.GOOGLE_CACHE_FILE))
        self.assertNotEqual(v2.common._CACHE_GOOGLE_FILE, original)

    def test_google_resolution_reuses_one_parallel_call_for_duplicate_urls(self):
        google = "https://news.google.com/articles/same"
        resolved = "https://example.com/materia"
        items = [news(1, link=google), news(2, link=google), news(3)]
        with patch.object(v2, "resolver_link_google_news", return_value=resolved) as resolver:
            v2.resolve_candidates(items)
        resolver.assert_called_once_with(google)
        self.assertEqual([item["link"] for item in items[:2]], [resolved, resolved])
        self.assertIn(v2.canonica(google), items[0]["aliases"])

    def test_cap_never_starves_new_items_or_discards_cached_eligible(self):
        old, fresh = news(1), news(2)
        old_id = v2.candidate_id("data_center", old)
        state = self.base_state([old])
        state["evaluations"][old_id] = {"assinatura": v2.evaluation_signature("data_center", old),
            "avaliacao": {"decisao": "elegivel", "bucket": "BR", "prioridade": 10, "fato": "old"}}
        with patch.object(v2, "load_state", return_value=state), \
             patch.object(v2, "coletar_itens_novos", return_value=[fresh]), \
             patch.object(v2, "enriquecer_fila"), patch.object(v2, "resolve_candidates"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=1)
        request = carregar_json(v2.REQUEST_FILE, {})
        self.assertEqual(len(request["candidates"]), 2)
        self.assertEqual(sum(c["precisa_avaliar"] for c in request["candidates"]), 1)

    def write_request(self, candidates):
        request = {"request_sha256": "request", "candidates": candidates}
        salvar_json(v2.REQUEST_FILE, request)
        return request

    def valid_input(self, **changes):
        request = {"schema": 1, "policy_version": v2.POLICY_VERSION,
            "run_id": "test", "generated_at": time.time(), "pilot": True,
            "send_enabled": False, "candidates": [], "truncated": {},
            "collection": {"queries": 10, "ok": 10, "failure_ratio": 0}}
        request.update(changes)
        request["request_sha256"] = v2.request_sha256(request)
        return request

    def test_load_input_copies_valid_snapshot_to_private_work_area(self):
        request = self.valid_input(candidates=[{"id": "a", "precisa_avaliar": True}])
        salvar_json(v2.INPUT_FILE, request)
        salvar_json(v2.STATE_FILE, self.base_state([]))
        v2.load_input(max_age_hours=6)
        self.assertEqual(carregar_json(v2.REQUEST_FILE, {}), request)

    def test_load_input_rejects_stale_snapshot(self):
        request = self.valid_input(generated_at=time.time() - 7 * 3600)
        salvar_json(v2.INPUT_FILE, request)
        salvar_json(v2.STATE_FILE, self.base_state([]))
        salvar_json(v2.REQUEST_FILE, {"old": True})
        with self.assertRaisesRegex(ValueError, "fora da janela"):
            v2.load_input(max_age_hours=6)
        self.assertFalse(v2.REQUEST_FILE.exists())

    def test_load_input_rejects_tampered_snapshot(self):
        request = self.valid_input()
        request["candidates"].append({"id": "adulterado"})
        salvar_json(v2.INPUT_FILE, request)
        salvar_json(v2.STATE_FILE, self.base_state([]))
        with self.assertRaisesRegex(ValueError, "integridade"):
            v2.load_input()

    def test_load_input_rejects_truncated_snapshot(self):
        request = self.valid_input(truncated={"data_center": {"novos_incluidos": 20}})
        salvar_json(v2.INPUT_FILE, request)
        salvar_json(v2.STATE_FILE, self.base_state([]))
        with self.assertRaisesRegex(ValueError, "truncada"):
            v2.load_input()

    def candidate(self, item, maximum=False, cached=None):
        cid = v2.candidate_id("data_center", item)
        return {"id": cid, "topico": "data_center", "titulo": item["titulo"],
                "fonte": item["fonte"], "link": v2.canonica(item["link"]),
                "trecho": item["resumo"], "publicado_em": item["publicado_em"],
                "fonte_maxima": maximum, "fonte_prioritaria": True,
                "assinatura": v2.evaluation_signature("data_center", item),
                "precisa_avaliar": cached is None, "avaliacao_cache": cached}

    def test_ranking_rejects_omitted_maximum_source(self):
        items = [news(1, "Brazil Journal", "https://braziljournal.com/a"), news(2)]
        candidates = [self.candidate(items[0], True), self.candidate(items[1])]
        self.write_request(candidates)
        salvar_json(v2.STATE_FILE, self.base_state(items))
        response = {"request_sha256": "request", "evaluations": [
            {"id": c["id"], "decisao": "elegivel", "bucket": "BR", "prioridade": 50, "fato": str(i)}
            for i, c in enumerate(candidates)],
            "selections": [{"id": candidates[1]["id"], "topico": "data_center", "bucket": "BR"}],
            "duplicates": {}}
        salvar_json(v2.RANKING_RESPONSE_FILE, response)
        with self.assertRaisesRegex(ValueError, "Fonte máxima"):
            v2.validate_ranking()

    def test_cached_evaluation_is_not_required_in_response(self):
        item = news()
        cached = {"decisao": "elegivel", "bucket": "BR", "prioridade": 80, "fato": "projeto"}
        candidate = self.candidate(item, cached=cached)
        self.write_request([candidate])
        salvar_json(v2.STATE_FILE, self.base_state([item]))
        salvar_json(v2.RANKING_RESPONSE_FILE, {"request_sha256": "request", "evaluations": [],
            "selections": [{"id": candidate["id"], "topico": "data_center", "bucket": "BR"}],
            "duplicates": {}})
        v2.validate_ranking()
        summary = carregar_json(v2.SUMMARY_REQUEST_FILE, {})
        self.assertEqual([x["id"] for x in summary["items"]], [candidate["id"]])

    def test_maximum_source_cannot_be_hidden_as_duplicate_of_common_source(self):
        items = [news(1, "Brazil Journal", "https://braziljournal.com/a"), news(2)]
        candidates = [self.candidate(items[0], True), self.candidate(items[1])]
        self.write_request(candidates)
        salvar_json(v2.STATE_FILE, self.base_state(items))
        evaluations = [{"id": c["id"], "decisao": "elegivel", "bucket": "BR",
                        "prioridade": 50, "fato": "mesmo"} for c in candidates]
        salvar_json(v2.RANKING_RESPONSE_FILE, {"request_sha256": "request",
            "evaluations": evaluations,
            "selections": [{"id": candidates[1]["id"], "topico": "data_center", "bucket": "BR"}],
            "duplicates": {candidates[0]["id"]: candidates[1]["id"]}})
        with self.assertRaisesRegex(ValueError, "fonte máxima"):
            v2.validate_ranking()

    def test_modified_article_signature_requires_new_evaluation(self):
        item = news()
        before = v2.evaluation_signature("data_center", item)
        item["resumo"] += " Nova licença ambiental foi concedida."
        self.assertNotEqual(before, v2.evaluation_signature("data_center", item))

    def test_finalize_generates_preview_without_sending_or_marking_sent(self):
        item = news()
        summary_request = {"schema": 1, "request_sha256": "request", "items": [{
            "id": "id1", "topico": "data_center", "bucket": "BR", "titulo": item["titulo"],
            "fonte": item["fonte"], "link": item["link"], "texto": item["resumo"]}],
            "summary_sha256": "summary"}
        salvar_json(v2.REQUEST_FILE, {"request_sha256": "request", "candidates": []})
        salvar_json(v2.SUMMARY_REQUEST_FILE, summary_request)
        salvar_json(v2.SUMMARY_RESPONSE_FILE, {"summary_sha256": "summary", "summaries": [
            {"id": "id1", "resumo": "O projeto recebeu autorização de conexão elétrica e divulgou sua capacidade."}]})
        v2.finalize()
        self.assertTrue(v2.PREVIEW_FILE.exists())
        self.assertFalse(carregar_json(v2.REPORT_FILE, {})["send_enabled"])
        self.assertIn("Ler matéria completa", v2.PREVIEW_FILE.read_text(encoding="utf-8"))
