import unittest
from unittest.mock import patch
from test_reliability import IsolatedState, item, client
import test_ranking
from test_ranking import decision
import digest_email as digest
from email_topic import verificar_tema
from email_api_errors import SaldoInsuficiente
from reliability import carregar_json


class TopicBoundaryTests(IsolatedState):
    run_model = test_ranking.RankingTests.run_model

    def test_real_curadoria_cases_cannot_be_promoted_by_maximum_source(self):
        for title, link in [
            ("Light anuncia encerramento da recuperação judicial", "https://eixos.com.br/energia-eletrica/light"),
            ("Brookfield avalia investimentos em baterias no Brasil", "https://megawhat.uol.com.br/baterias")]:
            candidate = item()
            candidate.update(titulo=title, resumo=title, link=link, aliases=[link])
            model = client([decision(0, score=100)])
            self.run_model([candidate], model).assert_not_called()
            model.messages.create.assert_not_called()
            audit = carregar_json(digest.AUDITORIA_FILE, {})
            self.assertEqual(next(r for r in audit["noticias"] if r["link"] == link)["resultado"], "sem_evidencia_tema")

    def test_brookfield_is_eligible_for_battery_evaluation_only(self):
        candidate = item(topics=["data_center", "baterias"])
        title = "Brookfield avalia investimentos em baterias no Brasil"
        candidate.update(titulo=title, resumo=title, link="https://megawhat.uol.com.br/baterias",
                         aliases=["https://megawhat.uol.com.br/baterias"])
        self.run_model([candidate], client([decision(0)]))
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(audit["resumo"]["data_center"]["BR"], 0)
        self.assertEqual(audit["resumo"]["baterias"]["BR"], 1)

    def test_transmission_story_remains_eligible_and_url_is_not_evidence(self):
        candidate = {"titulo": "Sobra energia para os data centers. O gargalo pode ser a transmissão", "resumo": ""}
        self.assertTrue(verificar_tema(candidate, "data_center")[0])
        self.assertFalse(verificar_tema({"titulo": "Light encerra recuperação", "resumo": "",
            "link": "https://example.com/data-centers", "fonte": "Data Centers News"}, "data_center")[0])

    def test_mixed_newsletter_without_article_text_stays_pending(self):
        candidate = {"titulo": "Data centers, fracking e etanolduto", "resumo": "Data centers, fracking e etanolduto",
            "link": "https://eixos.com.br/newsletters/dialogos/noticia"}
        self.assertFalse(verificar_tema(candidate, "data_center")[0])

    def test_credit_error_stops_calls_and_prevents_delivery(self):
        model = client([])
        model.messages.create.side_effect = RuntimeError("Your credit balance is too low to access the Anthropic API.")
        with patch.object(digest, "enviar_email") as send:
            with self.assertRaises(SaldoInsuficiente):
                self.run_model([item(i) for i in range(35)] + [item(90, ["carbono"])], model)
        send.assert_not_called()
        self.assertEqual(model.messages.create.call_count, 1)
        self.assertEqual(digest.carregar_estado(), set())
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertIn("saldo_insuficiente", audit["falhas"])
        self.assertTrue(all(r["resultado"] == "saldo_insuficiente" for r in audit["noticias"]))

    def test_credit_error_in_summary_is_not_silently_replaced_by_headline(self):
        model = client([])
        model.messages.create.side_effect = RuntimeError("Your credit balance is too low to access the Anthropic API.")
        with patch.object(digest, "buscar_texto_artigo", return_value="Reportagem sobre data centers e sua conexão à rede elétrica. " * 10):
            with self.assertRaises(SaldoInsuficiente):
                digest.resumir(model, [item()])

    def test_model_repeating_title_is_labelled_as_fallback(self):
        candidate = item()
        model = client([{"indice": 0, "resumo": candidate["titulo"]}])
        with patch.object(digest, "buscar_texto_artigo", return_value="Reportagem sobre data centers e sua conexão à rede elétrica. " * 10):
            digest.resumir(model, [candidate])
        self.assertTrue(candidate["resumo_final"].startswith("Trecho da fonte"))
        self.assertNotEqual(candidate["resumo_final"], candidate["titulo"])


class CarbonMarketTermsTests(unittest.TestCase):
    def test_carbon_projects_without_the_word_carbon_reach_the_ranking(self):
        # Mombak/BNDES (reflorestamento) foi descartada antes da IA por não dizer "carbono".
        for title in ["Mombak lança novo fundo e anuncia R$ 200 milhões do BNDES para reflorestamento na Amazônia",
                      "Brasil e Suíça assinam acordo do Artigo 6",
                      "Descarbonização da indústria avança com novo marco"]:
            self.assertTrue(verificar_tema({"titulo": title, "resumo": ""}, "carbono")[0], title)

    def test_unrelated_green_news_still_blocked(self):
        for title in ["Shopping distribui mudas no Dia da Árvore", "Rede de farmácias abre 50 lojas"]:
            self.assertFalse(verificar_tema({"titulo": title, "resumo": ""}, "carbono")[0], title)
