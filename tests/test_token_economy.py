from unittest import TestCase
from test_reliability import client, item
from test_ranking import decision
import email_ranking as ranking


class TokenEconomyTests(TestCase):
    def test_compact_rejection_is_auditable_and_reused_without_api(self):
        candidates = [item()]
        model = client({"avaliacoes": [{"indice": 0, "decisao": "fora_tema"}]})
        groups, failed = ranking.classificar(model, "data_center", "", candidates, "modelo")
        self.assertFalse(failed)
        self.assertEqual(groups, {"BR": [], "US": []})
        row = candidates[0]["avaliacoes"]["data_center"]
        self.assertIn("padronizada", row["motivo"])
        self.assertIsNone(row["bucket"])
        ranking.classificar(model, "data_center", "", candidates, "modelo")
        self.assertEqual(model.messages.create.call_count, 1)

    def test_compact_eligible_is_invalid(self):
        with self.assertRaises(ValueError):
            ranking.validar([{"indice": 0, "decisao": "elegivel"}], 1)

    def test_compact_rejection_survives_partial_response_and_retry_error(self):
        candidates = [item(0), item(1)]
        model = client([])
        model.messages.create.side_effect = [
            client([{"indice": 0, "decisao": "fora_tema"}]).messages.create.return_value,
            TimeoutError()]
        _, failed = ranking.classificar(model, "data_center", "", candidates, "modelo")
        self.assertTrue(failed)
        self.assertIn("padronizada", candidates[0]["cache_avaliacoes"]["data_center"]["avaliacao"]["motivo"])

    def test_google_opaque_link_is_not_sent_but_candidate_is_unchanged(self):
        candidate = item()
        candidate["link"] = "https://news.google.com/rss/articles/OPAQUE_ID_TEST"
        model = client([decision(0)])
        ranking.classificar(model, "data_center", "", [candidate], "modelo")
        prompt = model.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("OPAQUE_ID_TEST", prompt)
        self.assertIn("OPAQUE_ID_TEST", candidate["link"])
        self.assertIn(candidate["titulo"], prompt)
