import datetime
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from test_reliability import item, IsolatedState
import digest_email as digest
from reliability import carregar_json
import email_articles as articles
from email_source_health import atualizar, anotar_resultados


class ArticleTests(TestCase):
    def test_date_requires_explicit_publication_and_timezone(self):
        expected = datetime.datetime(2026, 9, 11, 10, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(articles.data_artigo('<meta property="article:published_time" content="2026-09-11T07:00:00-03:00">'), expected)
        self.assertEqual(articles.data_artigo('<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","datePublished":"2026-09-11T10:00:00Z"}]}</script>'), expected)
        for html in ['<meta property="article:modified_time" content="2026-09-11T10:00:00Z">',
                     '<script type="application/ld+json">{"@type":"Organization","datePublished":"2026-09-11T10:00:00Z"}</script>',
                     '<meta property="article:published_time" content="2026-09-11">']:
            self.assertIsNone(articles.data_artigo(html))

    def test_missing_date_and_text_recovered_and_reused(self):
        candidate = item()
        candidate["publicado_em"] = None
        html = '<meta property="article:published_time" content="2026-09-11T10:00:00Z">'
        with patch.object(articles.requests, "get", return_value=Mock(text=html)) as get, \
             patch.object(articles.trafilatura, "extract", return_value="Informação do artigo. " * 30):
            articles.complementar(candidate, lambda link: link, time.time())
            articles.complementar(candidate, lambda link: link, time.time())
        self.assertEqual(get.call_count, 1)
        self.assertIsNotNone(candidate["publicado_em"])
        self.assertEqual(candidate["origem_data"], "artigo")
        self.assertIn("Informação do artigo", candidate["resumo"])

    def test_existing_publication_is_never_replaced_by_newer_article_metadata(self):
        candidate = item()
        original = candidate["publicado_em"]
        with patch.object(articles.requests, "get", return_value=Mock(text='<meta property="article:published_time" content="2099-01-01T00:00:00Z">')), \
             patch.object(articles.trafilatura, "extract", return_value="Texto"):
            articles.complementar(candidate, lambda link: link, time.time())
        self.assertEqual(candidate["publicado_em"], original)

    def test_failed_request_is_retained_with_cooldown(self):
        candidate = item()
        with patch.object(articles.requests, "get", side_effect=TimeoutError("timeout")) as get:
            articles.complementar(candidate, lambda link: link, time.time())
            articles.complementar(candidate, lambda link: link, time.time())
        self.assertEqual(get.call_count, 1)
        self.assertEqual(candidate["resumo"], "Texto do feed")
        self.assertEqual(candidate["artigo"]["resultado"], "falha")

    def test_budget_prioritizes_never_attempted_and_skips_sent_and_old(self):
        fila = {str(i): {"item": item(i), "status": "pendente"} for i in range(5)}
        fila["0"]["item"]["artigo"] = {"tentado_em": time.time()}
        fila["1"]["status"] = "enviado"
        fila["2"]["item"]["publicado_em"] = time.time() - 10 * 86400
        with patch.object(articles, "MAX_ARTIGOS", 1), patch.object(articles, "complementar") as fetch:
            articles.enriquecer_fila(fila, lambda link: link, time.time())
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], fila["3"]["item"])


class SourceHealthTests(TestCase):
    def test_empty_searches_differ_from_failed_requests_and_history_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "sources.json")
            empty = {"topico": "baterias", "fonte": "MegaWhat", "consulta": "query", "resultado": "ok", "itens_rss": 0}
            for i in range(3):
                stats = atualizar(path, [empty], 10000000 + i)
            self.assertEqual(stats[0]["vazias_seguidas"], 3)
            self.assertIn("não provam", stats[0]["acao"])
            failed = {**empty, "resultado": "falha"}
            stats = atualizar(path, [failed], 10000000 + 31 * 86400)
            self.assertEqual(stats[0]["consultas"], 1)
            self.assertEqual(stats[0]["vazias_seguidas"], 0)
            self.assertIn("feed", stats[0]["acao"])

    def test_source_provenance_records_eligible_and_selected_independently(self):
        query = {"topico": "baterias", "fonte": "MegaWhat", "consulta": "query", "resultado": "ok", "itens_rss": 3}
        fila = {str(i): {"item": {"feeds_origem": ["query"], "topicos": ["baterias"]},
                "resultados": {"baterias": {"resultado": result}}}
                for i, result in enumerate(["selecionada", "sem_vaga", "fora_tema"])}
        anotar_resultados([query], fila)
        self.assertEqual(query["elegiveis"], 2)
        self.assertEqual(query["selecionadas"], 1)


class CaptureIntegrationTests(IsolatedState):
    def test_article_without_rss_date_reaches_ranking_and_audit(self):
        candidate = item()
        candidate["publicado_em"] = None
        date = datetime.datetime.now(datetime.timezone.utc).isoformat()
        response = Mock(text=f'<meta property="article:published_time" content="{date}">')
        seen = []
        def choose(client, topic, candidates):
            seen.extend(candidates)
            return {"BR": candidates, "US": []}, False
        with patch.object(digest, "enriquecer_fila", articles.enriquecer_fila), \
             patch.object(articles.requests, "get", return_value=response), \
             patch.object(articles.trafilatura, "extract", return_value="Texto recuperado da reportagem. " * 20):
            send = self.run_digest([candidate], choose)
        send.assert_called_once()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["origem_data"], "artigo")
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(audit["noticias"][0]["consulta_artigo"]["resultado"], "consultado")
        self.assertNotIn("texto", audit["noticias"][0]["consulta_artigo"])

    def test_source_report_is_readable_in_email_attachment_text(self):
        text = digest.relatorio_texto({"saude_fontes": [{"topico": "baterias", "fonte": "MegaWhat",
            "consultas": 3, "falhas": 0, "itens_rss": 7, "capturados": 4, "mesclados": 1,
            "elegiveis": 3, "selecionadas": 2, "acao": "Acompanhar."}]})
        self.assertIn("MegaWhat", text)
        self.assertIn("2 selecionadas", text)
