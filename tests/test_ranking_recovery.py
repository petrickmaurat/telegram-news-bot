from pathlib import Path
from unittest import TestCase
from test_reliability import item, client
from test_ranking import decision
import email_ranking as ranking


class RecoveryTests(TestCase):
    def test_schema_constrains_decision_geography_and_score(self):
        model = client({"avaliacoes": [decision(0)]})
        groups, failed = ranking.classificar(model, "data_center", "", [item()], "modelo")
        self.assertFalse(failed)
        self.assertEqual(len(groups["BR"]), 1)
        schema = model.messages.create.call_args.kwargs["output_config"]["format"]["schema"]
        fields = schema["properties"]["avaliacoes"]["items"]["properties"]
        self.assertEqual(fields["bucket"]["enum"], ["BR", "US"])
        self.assertEqual(fields["prioridade"]["enum"], list(range(101)))
        self.assertEqual(set(fields["decisao"]["enum"]), ranking.DECISOES)

    def test_invalid_response_is_recovered_once(self):
        model = client([])
        model.messages.create.side_effect = [
            client({"avaliacoes": [{**decision(0), "bucket": "INT"}]}).messages.create.return_value,
            client({"avaliacoes": [decision(0, bucket="US")]}).messages.create.return_value]
        groups, failed = ranking.classificar(model, "baterias", "", [item()], "modelo")
        self.assertFalse(failed)
        self.assertEqual(len(groups["US"]), 1)
        self.assertEqual(model.messages.create.call_count, 2)

    def test_valid_rows_survive_failed_recovery_and_are_cached(self):
        candidates = [item(0), item(1)]
        model = client({"avaliacoes": [decision(0), {**decision(1), "prioridade": "alta"}]})
        groups, failed = ranking.classificar(model, "data_center", "", candidates, "modelo")
        self.assertTrue(failed)
        self.assertEqual(groups["BR"], [candidates[0]])
        self.assertIn("data_center", candidates[0]["cache_avaliacoes"])
        self.assertNotIn("data_center", candidates[1]["avaliacoes"])
        self.assertEqual(model.messages.create.call_count, 2)

    def test_first_response_valid_rows_survive_transport_failure_during_recovery(self):
        candidates = [item(0), item(1)]
        model = client([])
        model.messages.create.side_effect = [client([decision(0)]).messages.create.return_value, TimeoutError()]
        groups, failed = ranking.classificar(model, "data_center", "", candidates, "modelo")
        self.assertTrue(failed)
        self.assertEqual(groups["BR"], [candidates[0]])
        self.assertEqual(model.messages.create.call_count, 2)

    def test_duplicate_indices_are_not_salvaged(self):
        model = client([decision(0), decision(0)])
        groups, failed = ranking.classificar(model, "data_center", "", [item(0), item(1)], "modelo")
        self.assertTrue(failed)
        self.assertEqual(groups, {"BR": [], "US": []})

    def test_schedule_includes_weekends_at_0717_brasilia(self):
        workflow = Path(".github/workflows/digest.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "17 10 * * *"', workflow)
