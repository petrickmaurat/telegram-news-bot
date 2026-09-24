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
        p = patch.object(v2, "enrich_delivery_candidates", side_effect=self.mark_ready)
        p.start()
        self.addCleanup(p.stop)
        v2.WORK_DIR.mkdir()

    def mark_ready(self, items, max_workers=12):
        for index, item in enumerate(items):
            link = item["link"]
            final = (f"https://example.com/resolved-{index}"
                     if "news.google.com" in link else link)
            item["artigo"] = {"link_final": final,
                "texto": " ".join(["conteudo"] * (v2.MIN_ARTICLE_WORDS + 5)),
                "resultado_entrega": "artigo_completo"}

    def base_state(self, items):
        return {"version": 1, "items": {v2.item_key(i): i for i in items},
                "evaluations": {}, "sent": []}

    def test_prepare_is_capped_and_does_not_call_ai_or_send(self):
        items = [news(i) for i in range(3)]
        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=items), \
             patch.object(v2, "enriquecer_fila"), \
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
             patch.object(v2, "enriquecer_fila") as enrich, \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        self.assertEqual(collect.call_args.kwargs["feed_workers"], 8)
        self.assertEqual(enrich.call_args.kwargs["max_workers"], 8)

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
             patch.object(v2, "enriquecer_fila"), \
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

    def test_dedupe_preserves_cache_identity_after_google_link_is_resolved(self):
        google = news(1, link="https://news.google.com/rss/articles/opaque")
        direct = news(1, link="https://reuters.com/final")
        google["aliases"] = [google["link"]]
        direct["aliases"] = [google["link"], direct["link"]]
        merged = v2.dedupe_same_article([google, direct])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["link"], google["link"])
        self.assertEqual(set(merged[0]["aliases"]), {google["link"], direct["link"]})

    def test_delivery_fetch_resolves_and_reads_without_changing_identity(self):
        google = "https://news.google.com/articles/opaque"
        item = news(link=google)
        original_key = v2.item_key(item)
        response = SimpleNamespace(text="<html>materia</html>", raise_for_status=lambda: None)
        full_text = " ".join(["informacao"] * (v2.MIN_ARTICLE_WORDS + 10))
        with patch.object(v2, "resolver_link_google_news", return_value="https://reuters.com/final"), \
             patch.object(v2.requests, "get", return_value=response), \
             patch.object(v2.trafilatura, "extract", return_value=full_text):
            v2._fetch_delivery_article(item, time.time())
        view = v2.delivery_view(item)
        self.assertEqual(item["link"], google)
        self.assertEqual(v2.item_key(item), original_key)
        self.assertEqual(view["link_final"], "https://reuters.com/final")
        self.assertEqual(view["nivel"], "artigo_completo")
        self.assertTrue(view["selecionavel"])

    def test_delivery_uses_disclosed_rss_excerpt_when_page_fails(self):
        item = news()
        item["resumo"] = " ".join(["trecho"] * (v2.MIN_EXCERPT_WORDS + 2))
        with patch.object(v2.requests, "get", side_effect=RuntimeError("bloqueado")):
            v2._fetch_delivery_article(item, time.time())
        view = v2.delivery_view(item)
        self.assertEqual(view["nivel"], "trecho_disponivel")
        self.assertTrue(view["selecionavel"])

    def test_delivery_keeps_thin_material_for_final_url_reading(self):
        item = news()
        with patch.object(v2.requests, "get", side_effect=RuntimeError("bloqueado")):
            v2._fetch_delivery_article(item, time.time())
        view = v2.delivery_view(item)
        self.assertEqual(view["nivel"], "insuficiente")
        self.assertTrue(view["selecionavel"])
        limited = v2.delivery_view(item, allow_limited=True)
        self.assertEqual(limited["nivel"], "trecho_limitado")
        self.assertTrue(limited["selecionavel"])

    def test_resolved_google_domain_restores_maximum_source_priority(self):
        item = news(source="Valor Econômico",
                    link="https://news.google.com/articles/valor")

        def resolve_as_valor(items, max_workers=12):
            for candidate_item in items:
                candidate_item["artigo"] = {
                    "link_final": "https://valor.globo.com/empresas/noticia.ghtml",
                    "texto": " ".join(["conteudo"] * (v2.MIN_ARTICLE_WORDS + 5))}

        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=[item]), \
             patch.object(v2, "enriquecer_fila"), \
             patch.object(v2, "enrich_delivery_candidates", side_effect=resolve_as_valor), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        candidate = carregar_json(v2.REQUEST_FILE, {})["candidates"][0]
        self.assertTrue(candidate["fonte_maxima"])
        self.assertEqual(candidate["dominio"], "valor.globo.com")

    def test_google_link_is_resolved_before_ranking_and_summary(self):
        google = "https://news.google.com/articles/opaque-token"
        item = news(1, link=google)
        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=[item]), \
             patch.object(v2, "enriquecer_fila"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        request = carregar_json(v2.REQUEST_FILE, {})
        candidate = request["candidates"][0]
        self.assertEqual(candidate["dominio"], "example.com")
        self.assertNotIn("link", candidate)
        self.assertTrue(candidate["leitura"]["selecionavel"])
        salvar_json(v2.RANKING_RESPONSE_FILE, {"request_sha256": request["request_sha256"],
            "evaluations": [{"id": candidate["id"], "decisao": "elegivel", "bucket": "BR",
                             "prioridade": 80, "fato": "projeto"}],
            "selections": [{"id": candidate["id"], "topico": "data_center", "bucket": "BR"}],
            "duplicates": {}})
        v2.validate_ranking()
        summary = carregar_json(v2.SUMMARY_REQUEST_FILE, {})
        self.assertEqual(summary["items"][0]["link"], "https://example.com/resolved-0")
        self.assertEqual(summary["items"][0]["base_resumo"], "artigo_completo")

    def test_cap_never_starves_new_items_or_discards_cached_eligible(self):
        old, fresh = news(1), news(2)
        old_id = v2.candidate_id("data_center", old)
        state = self.base_state([old])
        state["evaluations"][old_id] = {"assinatura": v2.evaluation_signature("data_center", old),
            "avaliacao": {"decisao": "elegivel", "bucket": "BR", "prioridade": 10, "fato": "old"}}
        with patch.object(v2, "load_state", return_value=state), \
             patch.object(v2, "coletar_itens_novos", return_value=[fresh]), \
             patch.object(v2, "enriquecer_fila"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=1)
        request = carregar_json(v2.REQUEST_FILE, {})
        self.assertEqual(len(request["candidates"]), 2)
        self.assertEqual(sum(c["precisa_avaliar"] for c in request["candidates"]), 1)

    def test_ranking_payload_is_compact_and_keeps_editorial_fields(self):
        old, fresh = news(1), news(2)
        old_id = v2.candidate_id("data_center", old)
        old["resumo"] = fresh["resumo"] = "palavra " * 200
        state = self.base_state([old])
        state["evaluations"][old_id] = {"assinatura": v2.evaluation_signature("data_center", old),
            "avaliacao": {"decisao": "elegivel", "bucket": "BR", "prioridade": 10, "fato": "old"}}
        with patch.object(v2, "load_state", return_value=state), \
             patch.object(v2, "coletar_itens_novos", return_value=[fresh]), \
             patch.object(v2, "enriquecer_fila"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        text = v2.REQUEST_FILE.read_text(encoding="utf-8")
        self.assertNotIn("\n  ", text)
        self.assertEqual(json.loads(text), carregar_json(v2.INPUT_FILE, {}))
        candidates = {c["precisa_avaliar"]: c for c in json.loads(text)["candidates"]}
        # Um candidato por linha: legível pelo Read sem indentação.
        lines = {line.rstrip(",") for line in text.splitlines()}
        for candidate in candidates.values():
            self.assertIn(json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")), lines)
        self.assertLessEqual(len(candidates[False]["trecho"]), v2.RANKING_EXCERPT_CACHED)
        self.assertGreater(len(candidates[True]["trecho"]), v2.RANKING_EXCERPT_CACHED)
        for candidate in candidates.values():
            self.assertEqual(set(candidate["leitura"]), {"nivel", "selecionavel"})
            self.assertNotIn("fonte_maxima", candidate)
            self.assertTrue(candidate["fonte_prioritaria"])
            self.assertEqual(candidate["dominio"], "reuters.com")
            self.assertTrue(candidate["titulo"] and candidate["fonte"])

    def test_items_outside_window_are_not_kept_in_state(self):
        old = news(1)
        old["publicado_em"] = time.time() - v2.IDADE_MAXIMA - 3600
        undated = news(2)
        undated["publicado_em"] = None
        with patch.object(v2, "load_state", return_value=self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=[old, undated, news(3)]), \
             patch.object(v2, "enriquecer_fila"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        kept = carregar_json(v2.STATE_FILE, {})["items"]
        self.assertNotIn(v2.item_key(old), kept)
        self.assertIn(v2.item_key(undated), kept)
        self.assertIn(v2.item_key(news(3)), kept)

    def test_email_google_queries_are_limited_to_window_without_touching_telegram(self):
        from common import TOPICOS
        from email_sources import configuracao_email
        config = configuracao_email(TOPICOS)
        from urllib.parse import parse_qs, urlsplit
        for topic, cfg in config.items():
            for feed in cfg["feeds"]:
                if feed["url"].startswith("https://news.google.com/rss/search"):
                    query = parse_qs(urlsplit(feed["url"]).query)["q"][0]
                    self.assertEqual(query.count("when:"), 1, feed["url"])
                    self.assertIn("when:3d", query)
        telegram_urls = [f["url"] for f in TOPICOS["data_center"]["feeds"]]
        self.assertTrue(all("when" not in url for url in telegram_urls))

    def prepare_and_load(self, items, state=None):
        with patch.object(v2, "load_state", return_value=state or self.base_state([])), \
             patch.object(v2, "coletar_itens_novos", return_value=items), \
             patch.object(v2, "enriquecer_fila"), \
             patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        return carregar_json(v2.REQUEST_FILE, {})

    def answer_batches(self, decide):
        manifest = carregar_json(v2.BATCH_DIR / "manifest.json", {})
        for batch in manifest["lotes"]:
            rows = carregar_json(batch["arquivo"], {})["candidates"]
            salvar_json(batch["resposta"], {"evaluations": [decide(row) for row in rows]})
        return manifest

    def test_batch_flow_reaches_summary_through_existing_validator(self):
        old = news(9)
        old_id = v2.candidate_id("data_center", old)
        state = self.base_state([old])
        state["evaluations"][old_id] = {"assinatura": v2.evaluation_signature("data_center", old),
            "avaliacao": {"decisao": "elegivel", "bucket": "BR", "prioridade": 95, "fato": "antigo"}}
        request = self.prepare_and_load([news(1), news(2), news(3)], state)
        v2.split_batches(size=2)
        batch_file = carregar_json(v2.BATCH_DIR / "data_center_01.json", {})
        self.assertIn("foco", batch_file)
        self.assertNotIn("leitura", batch_file["candidates"][0])
        manifest = self.answer_batches(lambda row: (
            {"id": row["id"], "decisao": "fora_tema"} if row["titulo"].endswith("3") else
            {"id": row["id"], "decisao": "elegivel", "bucket": "BR", "prioridade": 60,
             "fato": row["titulo"][-1]}))
        self.assertEqual(sum(b["quantidade"] for b in manifest["lotes"]), 3)
        v2.merge_batches()
        shortlist = carregar_json(v2.SHORTLIST_FILE, {})["topicos"]["data_center"]["BR"]
        # Avaliação em cache compete na rodada final junto com as novas.
        self.assertEqual(shortlist["elegiveis_total"], 3)
        self.assertEqual(shortlist["finalistas"][0]["id"], old_id)
        chosen = [row["id"] for row in shortlist["finalistas"]]
        salvar_json(v2.WORK_DIR / "selecao.json", {"selections": [
            {"id": cid, "topico": "data_center", "bucket": "BR", "assunto": f"assunto-{n}"}
            for n, cid in enumerate(chosen)]})
        v2.select(v2.WORK_DIR / "selecao.json")
        summary = carregar_json(v2.SUMMARY_REQUEST_FILE, {})
        self.assertEqual({i["id"] for i in summary["items"]}, set(chosen))
        self.assertEqual({i["assunto"] for i in summary["items"]}, {"assunto-0", "assunto-1", "assunto-2"})
        response = carregar_json(v2.RANKING_RESPONSE_FILE, {})
        self.assertEqual(response["request_sha256"], request["request_sha256"])
        self.assertEqual(len(response["evaluations"]), 3)

    def test_best_of_each_batch_reaches_final_round_despite_harsh_scores(self):
        self.prepare_and_load([news(n) for n in range(1, 41)])
        v2.split_batches(size=20)
        manifest = carregar_json(v2.BATCH_DIR / "manifest.json", {})
        harsh = {c["id"] for c in carregar_json(manifest["lotes"][1]["arquivo"], {})["candidates"]}
        self.answer_batches(lambda row: {"id": row["id"], "decisao": "elegivel", "bucket": "BR",
            "prioridade": 5 if row["id"] in harsh else 90, "fato": row["id"]})
        v2.merge_batches()
        finalists = carregar_json(v2.SHORTLIST_FILE, {})["topicos"]["data_center"]["BR"]["finalistas"]
        self.assertEqual(len([r for r in finalists if r["id"] in harsh]), v2.SHORTLIST_PER_BATCH)
        self.assertEqual(len(finalists), v2.SHORTLIST_PER_BUCKET + v2.SHORTLIST_PER_BATCH)

    def test_batch_rules_keep_topic_broad_and_focus_only_orders(self):
        # Regressão: sem estas regras, o foco elétrico rejeitou o REDATA como fora_tema.
        self.prepare_and_load([news(1)])
        v2.split_batches()
        batch = carregar_json(v2.BATCH_DIR / "data_center_01.json", {})
        self.assertIn("APENAS para ordenar", batch["regras"])
        self.assertIn("não tratar do setor elétrico", batch["regras"])
        self.assertIn("tributação", batch["regras"])
        self.assertIn("REDATA", batch["foco"])

    def test_final_round_puts_catalog_sources_before_common_ones(self):
        common_source = news(1, source="Blog", link="https://blog-desconhecido.test/a")
        self.prepare_and_load([common_source, news(2)])
        v2.split_batches()
        self.answer_batches(lambda row: {"id": row["id"], "decisao": "elegivel", "bucket": "BR",
            "prioridade": 80 if row["dominio"] == "blog-desconhecido.test" else 40, "fato": row["id"]})
        v2.merge_batches()
        finalists = carregar_json(v2.SHORTLIST_FILE, {})["topicos"]["data_center"]["BR"]["finalistas"]
        self.assertEqual([r["dominio"] for r in finalists], ["reuters.com", "blog-desconhecido.test"])
        self.assertEqual([r["prioritaria"] for r in finalists], [True, False])

    def test_sent_facts_are_remembered_for_next_final_round(self):
        self.finalized_edition()
        post, env = self.brevo(201, {"messageId": "m1"})
        with post, env:
            v2.send()
        facts = carregar_json(v2.STATE_FILE, {})["sent_facts"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["topico"], "data_center")
        self.assertIn("data center", facts[0]["titulo"])
        state = carregar_json(v2.STATE_FILE, {})
        with patch.object(v2, "load_state", return_value=state),              patch.object(v2, "coletar_itens_novos", return_value=[news(5)]),              patch.object(v2, "enriquecer_fila"), patch.object(v2, "salvar_cache_google"):
            v2.prepare(max_new_per_topic=0)
        salvar_json(v2.STATE_FILE, state)
        v2.split_batches()
        self.answer_batches(lambda row: {"id": row["id"], "decisao": "fora_tema"})
        v2.merge_batches()
        shortlist = carregar_json(v2.SHORTLIST_FILE, {})["topicos"]["data_center"]
        self.assertEqual(shortlist["ja_enviados"][0]["titulo"], facts[0]["titulo"])

    def test_same_subject_cannot_fill_topic_without_justification(self):
        rows = [{"id": "a", "topico": "data_center", "bucket": "BR", "assunto": "REDATA"},
                {"id": "b", "topico": "data_center", "bucket": "BR", "assunto": "redata"}]
        with self.assertRaises(ValueError) as error:
            v2.check_subject_diversity(rows)
        self.assertIn("Assunto repetido", str(error.exception))
        rows[1]["repeticao_justificada"] = "Veto presidencial derrubado: decisão nova do Congresso."
        v2.check_subject_diversity(rows)
        # O mesmo assunto em tópicos diferentes não conflita.
        v2.check_subject_diversity([rows[0], {"id": "c", "topico": "baterias", "bucket": "BR",
                                              "assunto": "redata"}])
        with self.assertRaises(ValueError):
            v2.check_subject_diversity([{"id": "d", "topico": "carbono", "bucket": "BR"}])

    def test_request_input_pushes_claude_branch_and_returns_to_current(self):
        calls = []

        def git(*args, capture=False):
            calls.append(args)
            return "v2-claude-routines" if capture else None

        with patch.object(v2, "_git", side_effect=git),              patch.object(v2, "TRIGGER_FILE", v2.WORK_DIR / "trigger.txt"):
            v2.request_input()
        request = carregar_json(v2.COLLECTION_REQUEST_FILE, {})
        self.assertTrue(request["branch"].startswith("claude/v2-coleta-"))
        self.assertIn(("push", "-q", "origin", request["branch"]), calls)
        self.assertEqual(calls[-2], ("checkout", "-q", "v2-claude-routines"))

    def test_wait_input_accepts_only_collection_after_request(self):
        salvar_json(v2.COLLECTION_REQUEST_FILE, {"since": 1000, "branch": "claude/v2-coleta-1000"})
        snapshots = iter([{"generated_at": 500}, {"generated_at": 1010}])
        merged = []

        def git(*args, capture=False):
            if args[0] == "show":
                return json.dumps(next(snapshots))
            if args[0] == "merge":
                merged.append(args)
            return None

        with patch.object(v2, "_git", side_effect=git), patch.object(v2.time, "sleep"):
            v2.wait_input(max_minutes=5)
        self.assertEqual(len(merged), 1)

    def test_wait_input_stops_with_code_3_when_time_is_up(self):
        salvar_json(v2.COLLECTION_REQUEST_FILE, {"since": 1000, "branch": "claude/v2-coleta-1000"})
        with patch.object(v2, "_git", side_effect=lambda *a, capture=False:
                          json.dumps({"generated_at": 1}) if a[0] == "show" else None),              patch.object(v2.time, "sleep"), self.assertRaises(SystemExit) as stop:
            v2.wait_input(max_minutes=0)
        self.assertEqual(stop.exception.code, 3)

    def test_merge_reports_missing_or_incomplete_batches(self):
        self.prepare_and_load([news(1), news(2), news(3)])
        v2.split_batches(size=2)
        manifest = carregar_json(v2.BATCH_DIR / "manifest.json", {})
        first = carregar_json(manifest["lotes"][0]["arquivo"], {})["candidates"]
        salvar_json(manifest["lotes"][0]["resposta"], {"evaluations": [
            {"id": first[0]["id"], "decisao": "fora_tema"}]})
        with self.assertRaises(ValueError) as error:
            v2.merge_batches()
        self.assertIn("2 lote(s)", str(error.exception))
        self.assertFalse(v2.RANKING_RESPONSE_FILE.exists())

    def test_merge_rejects_batches_from_another_collection(self):
        self.prepare_and_load([news(1)])
        v2.split_batches()
        request = carregar_json(v2.REQUEST_FILE, {})
        request["request_sha256"] = "outra"
        salvar_json(v2.REQUEST_FILE, request)
        with self.assertRaises(ValueError):
            v2.merge_batches()

    def write_request(self, candidates):
        request = {"request_sha256": "request", "candidates": candidates}
        salvar_json(v2.REQUEST_FILE, request)
        return request

    def valid_input(self, **changes):
        request = {"schema": v2.REQUEST_SCHEMA, "policy_version": v2.POLICY_VERSION,
            "run_id": "test", "generated_at": time.time(), "pilot": True,
            "send_enabled": False, "candidates": [], "truncated": {},
            "limits": {"data_center": {"BR": 1, "US": 0}},
            "collection": {"queries": 10, "ok": 10, "failure_ratio": 0}}
        request.update(changes)
        request["request_sha256"] = v2.request_sha256(request)
        return request

    def test_load_input_copies_valid_snapshot_to_private_work_area(self):
        request = self.valid_input(candidates=[{"id": "a", "precisa_avaliar": True,
            "topico": "data_center",
            "leitura": {"nivel": "artigo_completo", "selecionavel": True,
                        "link_resolvido": True}}])
        salvar_json(v2.INPUT_FILE, request)
        salvar_json(v2.STATE_FILE, self.base_state([]))
        v2.load_input(max_age_hours=6)
        self.assertEqual(carregar_json(v2.REQUEST_FILE, {}), request)

    def test_load_input_rejects_topic_without_enough_direct_links(self):
        request = self.valid_input(
            limits={"data_center": {"BR": 1, "US": 0},
                    "baterias": {"BR": 1, "US": 1}},
            candidates=[{"id": "a", "topico": "data_center", "precisa_avaliar": True,
                "leitura": {"nivel": "artigo_completo", "selecionavel": True,
                            "link_resolvido": True}}])
        salvar_json(v2.INPUT_FILE, request)
        salvar_json(v2.STATE_FILE, self.base_state([]))
        with self.assertRaisesRegex(ValueError, "baterias"):
            v2.load_input(max_age_hours=6)

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

    def candidate(self, item, maximum=False, cached=None, selectable=True):
        if selectable:
            item["artigo"] = {"link_final": item["link"],
                "texto": " ".join(["conteudo"] * (v2.MIN_ARTICLE_WORDS + 5))}
        cid = v2.candidate_id("data_center", item)
        return {"id": cid, "topico": "data_center", "titulo": item["titulo"],
                "fonte": item["fonte"], "link": v2.canonica(item["link"]),
                "trecho": item["resumo"], "publicado_em": item["publicado_em"],
                "fonte_maxima": maximum, "fonte_prioritaria": True,
                "leitura": {"nivel": "artigo_completo" if selectable else "insuficiente",
                            "palavras": v2.MIN_ARTICLE_WORDS + 5 if selectable else 10,
                            "link_resolvido": True, "selecionavel": True},
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

    def test_ranking_keeps_relevant_maximum_source_for_final_url_reading(self):
        unreadable = news(1, "Brazil Journal", "https://braziljournal.com/a")
        readable = news(2)
        candidates = [self.candidate(unreadable, True, selectable=False),
                      self.candidate(readable)]
        self.write_request(candidates)
        salvar_json(v2.STATE_FILE, self.base_state([unreadable, readable]))
        salvar_json(v2.RANKING_RESPONSE_FILE, {"request_sha256": "request",
            "evaluations": [
                {"id": candidates[0]["id"], "decisao": "elegivel", "bucket": "BR",
                 "prioridade": 90, "fato": "investimento"},
                {"id": candidates[1]["id"], "decisao": "fora_tema"}],
            "selections": [{"id": candidates[0]["id"], "topico": "data_center", "bucket": "BR"}],
            "duplicates": {}})
        v2.validate_ranking()
        summary = carregar_json(v2.SUMMARY_REQUEST_FILE, {})
        self.assertEqual([item["id"] for item in summary["items"]], [candidates[0]["id"]])
        self.assertTrue(summary["items"][0]["requer_leitura_url"])

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
        summary_request = {"schema": v2.REQUEST_SCHEMA, "request_sha256": "request", "items": [{
            "id": "id1", "topico": "data_center", "bucket": "BR", "titulo": item["titulo"],
            "fonte": item["fonte"], "link": item["link"], "texto": item["resumo"],
            "base_resumo": "artigo_completo"}],
            "summary_sha256": "summary"}
        salvar_json(v2.REQUEST_FILE, {"request_sha256": "request", "candidates": []})
        salvar_json(v2.SUMMARY_REQUEST_FILE, summary_request)
        salvar_json(v2.SUMMARY_RESPONSE_FILE, {"summary_sha256": "summary", "summaries": [
            {"id": "id1", "resumo": "O projeto recebeu autorização de conexão elétrica e divulgou sua capacidade."}]})
        v2.finalize()
        self.assertTrue(v2.PREVIEW_FILE.exists())
        self.assertFalse(carregar_json(v2.REPORT_FILE, {})["send_enabled"])
        self.assertIn("Ler matéria completa", v2.PREVIEW_FILE.read_text(encoding="utf-8"))

    def finalized_edition(self):
        item = news()
        cid = v2.candidate_id("data_center", item)
        salvar_json(v2.STATE_FILE, self.base_state([item]))
        summary_request = {"schema": v2.REQUEST_SCHEMA, "request_sha256": "request", "items": [{
            "id": cid, "topico": "data_center", "bucket": "BR", "titulo": item["titulo"],
            "fonte": item["fonte"], "link": item["link"], "texto": item["resumo"],
            "base_resumo": "artigo_completo"}],
            "summary_sha256": "summary"}
        salvar_json(v2.REQUEST_FILE, {"request_sha256": "request", "candidates": []})
        salvar_json(v2.SUMMARY_REQUEST_FILE, summary_request)
        salvar_json(v2.SUMMARY_RESPONSE_FILE, {"summary_sha256": "summary", "summaries": [
            {"id": cid, "resumo": "O projeto recebeu autorização de conexão elétrica e divulgou sua capacidade."}]})
        v2.finalize()
        return item

    def brevo(self, status_code, body=None):
        response = SimpleNamespace(status_code=status_code, json=lambda: body or {})
        env = {"BREVO_API_KEY": "k", "EMAIL_REMETENTE": "a@b.c", "EMAIL_DESTINO": "d@e.f"}
        return patch.object(v2.requests, "post", return_value=response), patch.dict(v2.os.environ, env)

    def test_send_marks_selection_as_sent_and_never_repeats_edition(self):
        item = self.finalized_edition()
        report = carregar_json(v2.REPORT_FILE, {})
        self.assertIn("Panorama Data Centers", report["subject"])
        self.assertNotIn("piloto", v2.PREVIEW_FILE.read_text(encoding="utf-8"))
        post, env = self.brevo(201, {"messageId": "m1"})
        with post as sent, env:
            v2.send()
            v2.send()
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(sent.call_args.kwargs["json"]["subject"], report["subject"])
        state = carregar_json(v2.STATE_FILE, {})
        self.assertIn(item["link"], state["sent"])
        self.assertNotIn("delivery_pending", state)
        self.assertEqual(carregar_json(v2.REPORT_FILE, {})["email"]["status"], "enviado")

    def test_rejected_send_marks_nothing_and_uncertain_send_blocks_repeat(self):
        item = self.finalized_edition()
        post, env = self.brevo(400)
        with post, env, self.assertRaises(RuntimeError):
            v2.send()
        state = carregar_json(v2.STATE_FILE, {})
        self.assertNotIn(item["link"], state.get("sent", []))
        self.assertNotIn("delivery_pending", state)
        post, env = self.brevo(502)
        with post, env, self.assertRaises(RuntimeError):
            v2.send()
        post, env = self.brevo(201, {"messageId": "m1"})
        with post as sent, env:
            v2.send()
        self.assertEqual(sent.call_count, 0)
        self.assertIn("delivery_pending", carregar_json(v2.STATE_FILE, {}))

    def test_send_without_credentials_sends_nothing(self):
        self.finalized_edition()
        with patch.dict(v2.os.environ, {}, clear=True), \
             patch.object(v2.requests, "post") as sent, self.assertRaises(RuntimeError):
            v2.send()
        sent.assert_not_called()

    def test_pilot_preview_rejects_unresolved_google_link(self):
        item = news(link="https://news.google.com/articles/opaque-token")
        summary_request = {"schema": v2.REQUEST_SCHEMA, "request_sha256": "request", "items": [{
            "id": "id1", "topico": "data_center", "bucket": "BR", "titulo": item["titulo"],
            "fonte": item["fonte"], "link": item["link"], "texto": item["resumo"],
            "base_resumo": "trecho_disponivel"}],
            "summary_sha256": "summary"}
        salvar_json(v2.REQUEST_FILE, {"request_sha256": "request", "candidates": []})
        salvar_json(v2.SUMMARY_REQUEST_FILE, summary_request)
        salvar_json(v2.SUMMARY_RESPONSE_FILE, {"summary_sha256": "summary", "summaries": [
            {"id": "id1", "resumo": "O projeto recebeu autorização de conexão elétrica e divulgou sua capacidade."}]})
        with self.assertRaisesRegex(ValueError, "Link"):
            v2.finalize()

    def test_excerpt_is_disclosed_in_preview(self):
        item = news()
        summary_request = {"schema": v2.REQUEST_SCHEMA, "request_sha256": "request", "items": [{
            "id": "id1", "topico": "data_center", "bucket": "BR", "titulo": item["titulo"],
            "fonte": item["fonte"], "link": item["link"], "texto": item["resumo"],
            "base_resumo": "trecho_disponivel"}], "summary_sha256": "summary"}
        salvar_json(v2.REQUEST_FILE, {"request_sha256": "request", "candidates": []})
        salvar_json(v2.SUMMARY_REQUEST_FILE, summary_request)
        salvar_json(v2.SUMMARY_RESPONSE_FILE, {"summary_sha256": "summary", "summaries": [
            {"id": "id1", "resumo": "O projeto recebeu autorização e informou capacidade."}]})
        v2.finalize()
        self.assertIn("trecho disponibilizado pela fonte",
                      v2.PREVIEW_FILE.read_text(encoding="utf-8"))

    def test_unavailable_final_url_is_disclosed_in_preview(self):
        item = news()
        summary_request = {"schema": v2.REQUEST_SCHEMA, "request_sha256": "request", "items": [{
            "id": "id1", "topico": "data_center", "bucket": "BR", "titulo": item["titulo"],
            "fonte": item["fonte"], "link": item["link"], "texto": item["resumo"],
            "base_resumo": "insuficiente", "requer_leitura_url": True}],
            "summary_sha256": "summary"}
        salvar_json(v2.REQUEST_FILE, {"request_sha256": "request", "candidates": []})
        salvar_json(v2.SUMMARY_REQUEST_FILE, summary_request)
        salvar_json(v2.SUMMARY_RESPONSE_FILE, {"summary_sha256": "summary", "summaries": [{
            "id": "id1", "resumo": "A fonte anunciou um novo projeto no setor.",
            "leitura_url": "indisponivel"}]})
        v2.finalize()
        self.assertIn("não permitiu leitura integral",
                      v2.PREVIEW_FILE.read_text(encoding="utf-8"))
