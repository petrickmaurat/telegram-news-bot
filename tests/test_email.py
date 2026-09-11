"""Comportamento exclusivo do digest e compatibilidade com o Telegram."""
import calendar
from html.parser import HTMLParser
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from test_reliability import IsolatedState, item, client
import common
import digest_email as digest
import email_policy as policy
from reliability import carregar_json, salvar_json


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)


class EmailTests(IsolatedState):
    def test_email_escapes_all_external_html(self):
        noticia = item()
        attack = '<img src=x onerror="bad()"> & "texto"'
        noticia.update(titulo=attack, fonte=attack, resumo_final=attack,
                       link='https://reuters.com/a" onclick="bad()?x=1&y=2')
        html = digest.montar_html({"data_center": {"BR": [noticia], "US": []}}, attack)
        parsed = Links()
        parsed.feed(html)
        self.assertNotIn("img", parsed.tags)
        self.assertFalse(any(k.startswith("on") for k, v in parsed.attrs))
        self.assertIn("&lt;img", html)
        self.assertIn("&amp;", html)
        self.assertEqual(noticia["titulo"], attack)  # não muta o item original
        for url in ["javascript:alert(1)", "https://reuters.com@evil.test/a", "https://news.google.com/rss/articles/unresolved"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                digest._card_noticia({**noticia, "link": url}, digest.TEMA["data_center"])

    def test_collection_does_not_decode_when_resolver_false(self):
        google = "https://news.google.com/rss/articles/abc"
        topics = {"data_center": dict(keywords=["data center"], feeds=[dict(url="feed", origem="BR")])}
        feed = NS(bozo=False, feed={}, entries=[dict(link=google, title="data center", published_parsed=time.gmtime())])
        with patch.object(common, "TOPICOS", topics), patch.object(common, "_parse_feed", return_value=feed), \
             patch.object(common, "gnewsdecoder") as decoder:
            result = common.coletar_itens_novos(set(), resolver=False)
            decoder.assert_not_called()
            self.assertEqual(result[0]["link"], google)
            self.assertTrue(policy.recente(result[0]))
            with patch.dict(common._cache_google, {google: item()["link"]}):
                cached = common.coletar_itens_novos(set(), resolver=False)
                self.assertEqual(cached[0]["link"], item()["link"])
                decoder.assert_not_called()

    def test_topic_failure_still_sends_carbon_and_reports_partial_failure(self):
        candidates = [item(1), item(2, ["carbono"])]
        calls = []
        def choose(c, topic, batch):
            calls.append(topic)
            if topic == "data_center":
                raise RuntimeError("API falhou")
            return {"BR": [batch[0]], "US": []}
        with patch.object(digest, "enviar_email") as send, \
             patch.object(digest, "coletar_itens_novos", return_value=candidates), \
             patch.object(digest.anthropic, "Anthropic", return_value=client([])), \
             patch.object(digest, "selecionar", side_effect=choose), \
             patch.object(digest, "resumir"), patch.object(digest, "montar_html", return_value="html"):
            with self.assertRaisesRegex(RuntimeError, "parcial enviado"):
                digest.rodar_digest()
            send.assert_called_once()
        self.assertEqual(calls, ["data_center", "carbono"])
        self.assertEqual(digest.carregar_estado(), {item(2)["link"]})
        self.assertEqual(carregar_json(digest.FILA_FILE, {})[item(1)["link"]]["status"], "pendente")
        self.assertIsNone(carregar_json(digest.INCERTO_FILE, {}))

    def test_resolves_reserves_and_prioritizes_verified_domains(self):
        candidates = [item(i) for i in range(20)]
        for n, noticia in enumerate(candidates):
            noticia["link"] = f"https://news.google.com/rss/articles/{n}"
            noticia["aliases"] = [noticia["link"]]
        resolved = []
        def resolve(url):
            resolved.append(url)
            return "https://regional.test/a" if url.endswith("/0") else item(int(url.rsplit('/', 1)[-1]))["link"]
        with patch.object(digest, "resolver_link_google_news", side_effect=resolve):
            send = self.run_digest(candidates, lambda c, t, batch: {"BR": batch, "US": []})
        send.assert_called_once()
        self.assertLessEqual(len(resolved), 4)
        self.assertEqual(len(digest.carregar_estado()), 6)
        fila = carregar_json(digest.FILA_FILE, {})
        self.assertEqual(sum(r["status"] == "enviado" for r in fila.values()), 3)

    def test_google_duplicate_is_removed_after_resolution(self):
        first, second, third = item(1), item(2), item(3)
        second["link"] = "https://news.google.com/rss/articles/duplicate"
        second["aliases"] = [second["link"]]
        batches = []
        def choose(c, t, batch):
            batches.append(batch)
            return {"BR": batch, "US": []}
        with patch.object(digest, "resolver_link_google_news", side_effect=lambda url: first["link"] if "google.com" in url else url):
            self.run_digest([first, second, third], choose)
        self.assertTrue({first["link"], third["link"]}.issubset(digest.carregar_estado()))
        self.assertEqual(sum(r["status"] == "enviado" for r in carregar_json(digest.FILA_FILE, {}).values()), 2)
        self.assertEqual(len(batches), 1)

    def test_publication_window_and_no_update_fallback(self):
        now = time.time()
        for delta, accepted in [(0, True), (71 * 3600, True), (73 * 3600, False), (-3600, False)]:
            self.assertEqual(policy.recente({"publicado_em": now - delta}, now), accepted)
        for value in [None, "yesterday", True, float("nan")]:
            self.assertFalse(policy.recente({"publicado_em": value}, now))
        self.assertIsNone(policy.data_publicacao({"updated_parsed": time.gmtime()}))
        self.assertIsNone(policy.data_publicacao({"published": "invalid"}))
        self.assertEqual(policy.data_publicacao({"published": "Fri, 11 Sep 2026 09:00:00 -0300"}),
                         calendar.timegm((2026, 9, 11, 12, 0, 0)))

    def test_old_undated_future_never_reach_ai(self):
        candidates = [item(i) for i in range(4)]
        candidates[0]["publicado_em"] = time.time() - 100 * 3600
        candidates[1]["publicado_em"] = None
        candidates[2]["publicado_em"] = time.time() + 86400
        choose = Mock(side_effect=lambda c, t, batch: {"BR": batch, "US": []})
        self.run_digest(candidates, choose)
        self.assertEqual([i["link"] for i in choose.call_args.args[2]], [item(3)["link"]])
        self.assertEqual(digest.carregar_estado(), {item(3)["link"]})

    def test_terminal_records_compact_then_prune_without_removing_pending(self):
        now = time.time()
        fila = {}
        for n, days in enumerate([8, 31, 40]):
            fila[str(n)] = dict(item=item(n), status="pendente" if n == 2 else "rejeitado", encerrado=now - days * 86400)
        policy.podar_fila(fila, now)
        self.assertNotIn("titulo", fila["0"]["item"])
        self.assertIn("aliases", fila["0"]["item"])
        self.assertNotIn("1", fila)
        self.assertIn("titulo", fila["2"]["item"])

    def test_title_only_and_short_excerpt_do_not_call_model(self):
        candidates = [item(1), item(2)]
        candidates[0]["resumo"] = ""
        model = client([])
        with patch.object(digest, "buscar_texto_artigo", return_value=""):
            digest.resumir(model, candidates)
        model.messages.create.assert_not_called()
        self.assertEqual(candidates[0]["resumo_final"], candidates[0]["titulo"])
        self.assertEqual(candidates[1]["resumo_final"], "Texto do feed")
