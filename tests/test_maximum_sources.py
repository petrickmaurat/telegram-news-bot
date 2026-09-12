from unittest.mock import patch
from test_reliability import item, client, IsolatedState
import test_ranking
from test_ranking import decision
from email_sources import fonte_maxima
import digest_email as digest


class MaximumSourceTests(IsolatedState):
    run_model = test_ranking.RankingTests.run_model

    def test_maximum_reserve_precedes_ordinary_finalists(self):
        candidates = [item(i) for i in range(4)]
        candidates[3].update(link="https://braziljournal.com/reserva", aliases=["https://braziljournal.com/reserva"])
        def choose(client, topic, batch):
            for i, c in enumerate(batch):
                c["avaliacoes"] = {topic: {**decision(i, score=100 if i < 3 else 1), "rodada": 2 if i < 3 else 0}}
            return {"BR": batch, "US": []}, False
        self.run_digest(candidates, choose)
        self.assertIn(candidates[3]["link"], digest.carregar_estado())
    def test_maximum_source_beats_score_and_finalist_status(self):
        candidates = [item(i) for i in range(4)]
        candidates[3]["link"] = "https://braziljournal.com/reportagem"
        candidates[3]["aliases"] = [candidates[3]["link"]]
        self.run_model(candidates, client([decision(i, score=100 if i < 3 else 1) for i in range(4)]))
        self.assertIn(candidates[3]["link"], digest.carregar_estado())
        self.assertEqual(len(digest.carregar_estado()), 3)

    def test_four_maximum_sources_compete_for_three_slots(self):
        candidates = [item(i) for i in range(4)]
        for c, domain in zip(candidates, ["braziljournal.com", "valor.globo.com", "eixos.com.br", "agenciainfra.com"]):
            c["link"] = "https://" + domain + "/noticia"
            c["aliases"] = [c["link"]]
        self.run_model(candidates, client([decision(i, score=i * 10) for i in range(4)]))
        self.assertNotIn(candidates[0]["link"], digest.carregar_estado())
        self.assertEqual(len(digest.carregar_estado()), 3)

    def test_google_link_cannot_hide_maximum_source_after_quota(self):
        candidates = [item(i) for i in range(4)]
        candidates[3]["link"] = "https://news.google.com/rss/articles/maximum"
        candidates[3]["aliases"] = [candidates[3]["link"]]
        destination = "https://megawhat.uol.com.br/noticia"
        with patch.object(digest, "resolver_link_google_news", side_effect=lambda u: destination if "news.google.com" in u else u):
            self.run_model(candidates, client([decision(i, score=100-i*30) for i in range(4)]))
        self.assertIn(destination, digest.carregar_estado())

    def test_domains_are_exact_or_subdomains_and_brasil_energia_is_not_maximum(self):
        for host in ["braziljournal.com", "megawhat.uol.com.br", "valor.globo.com", "pipelinevalor.globo.com", "assinantes.agenciainfra.com", "eixos.com.br"]:
            self.assertTrue(fonte_maxima({"link": "https://" + host + "/a"}))
        for host in ["brasilenergia.com.br", "braziljournal.com.evil.example", "fakevalor.globo.com"]:
            self.assertFalse(fonte_maxima({"link": "https://" + host + "/a"}))

    def test_maximum_source_outside_topic_is_not_selected(self):
        candidate = item()
        candidate["link"] = "https://braziljournal.com/noticia"
        candidate["aliases"] = [candidate["link"]]
        send = self.run_model([candidate], client([decision(0, code="fora_tema")]))
        send.assert_not_called()
