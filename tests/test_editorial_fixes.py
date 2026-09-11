from unittest import TestCase
from unittest.mock import patch
from test_reliability import item, client
from test_ranking import decision
import email_ranking as ranking
from email_dedup import comparador
from email_language import idioma_permitido
from email_sources import configuracao_email
from common import TOPICOS, contem_palavra_chave
import common
from types import SimpleNamespace as NS


class EditorialFixTests(TestCase):
    def test_finalists_precede_reserves_even_with_lower_final_scores(self):
        candidates = [item(i) for i in range(35)]
        model = client([])
        model.messages.create.side_effect = [
            client([decision(i, score=100-i) for i in range(30)]).messages.create.return_value,
            client([decision(i, score=50-i) for i in range(5)]).messages.create.return_value,
            client([decision(i, score=1) for i in range(15)]).messages.create.return_value]
        groups, failed = ranking.classificar(model, "data_center", "foco", candidates, "modelo")
        self.assertFalse(failed)
        self.assertTrue(all(c["avaliacoes"]["data_center"]["rodada"] == 1 for c in groups["BR"][:15]))
        self.assertEqual(candidates[0]["cache_avaliacoes"]["data_center"]["avaliacao"]["prioridade"], 100)

    def test_editorial_cache_invalidates_and_failed_refresh_cannot_use_stale_result(self):
        candidates = [item()]
        model = client([decision(0)])
        ranking.classificar(model, "data_center", "antigo", candidates, "modelo")
        ranking.classificar(model, "data_center", "antigo", candidates, "modelo")
        self.assertEqual(model.messages.create.call_count, 1)
        model.messages.create.side_effect = RuntimeError("API indisponível")
        groups, failed = ranking.classificar(model, "data_center", "novo", candidates, "modelo")
        self.assertTrue(failed)
        self.assertEqual(groups, {"BR": [], "US": []})
        self.assertNotIn("data_center", candidates[0]["avaliacoes"])

    def test_different_manufacturers_and_new_developments_are_not_duplicates(self):
        model = client({"mesmo_fato": False})
        compare = comparador(model, "modelo")
        self.assertFalse(compare("CATL reduz preço de baterias em 20%", "BYD reduz preço de baterias em 15%"))
        self.assertFalse(compare("MME confirma leilão de baterias", "MME suspende leilão de baterias"))
        self.assertEqual(model.messages.create.call_count, 2)

    def test_semantic_confirmation_is_cached_and_failure_preserves_news(self):
        model = client({"mesmo_fato": True})
        compare = comparador(model, "modelo")
        a, b = "Google investe em data center no Ceará", "Google anuncia investimento em data center no Ceará"
        self.assertTrue(compare(a, b))
        self.assertTrue(compare(b, a))
        self.assertEqual(model.messages.create.call_count, 1)
        model.messages.create.side_effect = TimeoutError()
        self.assertFalse(comparador(model, "modelo")(a, b))

    def test_language_gate_accepts_english_and_portuguese_and_blocks_chinese_spanish(self):
        for title in ["CATL cuts cell prices as the global battery war intensifies",
                      "Aneel define preço-teto do leilão de baterias marcado para 2027"]:
            self.assertTrue(idioma_permitido({"titulo": title})[0], title)
        for title in ["CATL 宁德时代发布全新储能电池技术", "El gobierno anunció una nueva subasta de baterías para mejorar el almacenamiento de energía"]:
            self.assertFalse(idioma_permitido({"titulo": title})[0], title)

    def test_missing_language_detector_preserves_pending_candidate(self):
        with patch("email_language.detector", side_effect=ImportError("missing")):
            allowed, reason = idioma_permitido({"titulo": "Battery prices decline worldwide"})
        self.assertFalse(allowed)
        self.assertIn("pendente", reason)

    def test_manufacturers_are_email_keywords_only(self):
        keywords = configuracao_email(TOPICOS)["baterias"]["keywords"]
        for title in ["BYD cuts cell prices", "EVE Energy opens new gigafactory", "Gotion expands production", "Hithium cuts costs"]:
            self.assertTrue(contem_palavra_chave(title, keywords))
        self.assertNotIn("BYD", TOPICOS["baterias"]["keywords"])

    def test_targeted_capture_retains_short_headlines_and_merges_richer_summary(self):
        url = "https://news.google.com/rss/search?q=battery"
        config = {"baterias": {"keywords": ["battery"], "feeds": [
            {"url": url, "origem": "US"}, {"url": "https://example.com/feed", "origem": "US"}]}}
        targeted = NS(bozo=False, feed={}, entries=[{"title": "BYD cuts cell prices",
            "link": "https://reuters.com/price", "summary": "Short"}])
        direct = NS(bozo=False, feed={}, entries=[{"title": "BYD cuts cell prices",
            "link": "https://reuters.com/price", "summary": "A much richer battery price report"},
            {"title": "Football result", "link": "https://reuters.com/sport"}])
        reports = []
        with patch.object(common, "_parse_feed", side_effect=lambda u: targeted if u == url else direct), \
             patch.object(common, "salvar_cache_google"), patch.object(common, "_cache_google", {}):
            captured = common.coletar_itens_novos(set(), resolver=False, configuracao=config, relatorio_fontes=reports)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["resumo"], "A much richer battery price report")
        self.assertEqual(reports[0]["contagem"]["capturados"], 1)
        self.assertEqual(reports[1]["contagem"]["mesclados"], 1)
        self.assertEqual(reports[1]["contagem"]["fora_keywords"], 1)
