import base64
import json
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from test_reliability import IsolatedState, item, client
import digest_email as digest
import email_ranking as ranking
from email_sources import FONTES, NOMES, configuracao_email, fonte_prioritaria
from reliability import carregar_json, salvar_json


def decision(i, code="elegivel", score=0, bucket="BR"):
    return {"indice": i, "decisao": code, "prioridade": score, "bucket": bucket,
            "fato": f"fato-{i}", "motivo": "Assunto do tema com baixo impacto; elegível para completar as vagas."}


class RankingTests(IsolatedState):
    def run_model(self, candidates, model):
        with patch.object(digest, "coletar_itens_novos", return_value=candidates), \
             patch.object(digest.anthropic, "Anthropic", return_value=model), \
             patch.object(digest, "resumir"), patch.object(digest, "montar_html", return_value="html"), \
             patch.object(digest, "enviar_email") as send:
            digest.rodar_digest()
        return send

    def test_three_zero_priority_brazilian_items_fill_three_slots(self):
        candidates = [item(i) for i in range(4)]
        self.run_model(candidates, client([decision(i) for i in range(4)]))
        self.assertEqual(len(digest.carregar_estado()), 3)
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(audit["resumo"]["data_center"]["BR"], 3)
        self.assertEqual(sum(r["resultado"] == "sem_vaga" for r in audit["noticias"]), 1)

    def test_priority_site_first_and_other_sites_complete_quota(self):
        candidates = [item(i) for i in range(3)]
        candidates[1].update(link="https://regional.example/a", aliases=["https://regional.example/a"])
        candidates[2].update(link="https://regional.example/b", aliases=["https://regional.example/b"])
        self.run_model(candidates, client([decision(0), decision(1, score=90), decision(2, score=5)]))
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(audit["resumo"]["data_center"]["BR"], 3)
        self.assertEqual(len(digest.carregar_estado()), 3)

    def test_carbon_low_electricity_priority_is_included(self):
        candidates = [item(1, ["carbono"])]
        candidates[0]["titulo"] = "EU lawmaker proposes more carbon market investments in industry"
        self.run_model(candidates, client([decision(0, bucket="US")]))
        self.assertEqual(carregar_json(digest.AUDITORIA_FILE, {})["resumo"]["carbono"]["US"], 1)

    def test_every_rejection_has_editorial_reason_in_audit(self):
        row = decision(0, "fora_tema")
        row["motivo"] = "O texto discute baterias residenciais, sem relação com data centers."
        send = self.run_model([item()], client([row]))
        send.assert_not_called()
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(audit["noticias"][0]["resultado"], "fora_tema")
        self.assertEqual(audit["noticias"][0]["motivo"], row["motivo"])
        self.assertIn(row["motivo"], digest.relatorio_texto(audit))

    def test_invalid_missing_reason_or_low_priority_rejection_is_not_accepted(self):
        for row in [{**decision(0), "motivo": ""}, {**decision(0), "decisao": "baixa_prioridade"}]:
            with self.assertRaises(ValueError):
                ranking.validar([row], 1)
        with self.assertRaises(ValueError):
            ranking.validar([], 1)

    def test_more_than_one_model_batch_is_fully_evaluated(self):
        candidates = [item(i) for i in range(35)]
        model = client([])
        first = client([decision(i) for i in range(30)]).messages.create.return_value
        second = client([{**decision(i), "fato": f"fato-{i+30}"} for i in range(5)]).messages.create.return_value
        model.messages.create.side_effect = [first, second]
        ranking.classificar(model, "data_center", "", candidates, digest.MODELO)
        self.assertEqual(model.messages.create.call_count, 2)
        self.assertTrue(all("data_center" in c["avaliacoes"] for c in candidates))
        ranking.classificar(model, "data_center", "", candidates, digest.MODELO)
        self.assertEqual(model.messages.create.call_count, 2)  # cache de avaliação

    def test_duplicate_facts_use_reserves_until_quota(self):
        rows = [decision(i, score=100-i) for i in range(5)]
        rows[1]["fato"] = rows[0]["fato"]
        rows[2]["fato"] = rows[0]["fato"]
        self.run_model([item(i) for i in range(5)], client(rows))
        self.assertEqual(len(digest.carregar_estado()), 3)
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(sum(r["resultado"] == "duplicada" for r in audit["noticias"]), 2)

    def test_country_is_model_fact_classification_not_feed_origin(self):
        noticia = item()
        noticia["fonte"] = "CNN Brasil"
        noticia["titulo"] = "Emirados revisam plano para data centers"
        self.run_model([noticia], client([decision(0, bucket="US")]))
        self.assertEqual(carregar_json(digest.AUDITORIA_FILE, {})["resumo"]["data_center"], {"BR": 0, "US": 1})

    def test_all_priority_sites_are_queried_for_both_topics(self):
        config = configuracao_email(digest.TOPICOS)
        for topic in config.values():
            targeted = [f for f in topic["feeds"] if "veiculo_monitorado" in f]
            self.assertEqual(len(targeted), len(FONTES))
            for source, feed in zip(FONTES, targeted):
                query = parse_qs(urlsplit(feed["url"]).query)["q"][0]
                self.assertIn("site:" + source, query)
        for site in ["agenciainfra.com", "gov.br/mme", "ri.sanepar.com.br", "brasilenergia.com.br", "eixos.com.br"]:
            self.assertTrue(fonte_prioritaria({"link": "https://" + site + "/noticia"}))
        self.assertIsNone(fonte_prioritaria({"link": "https://gov.br/outro/noticia"}))

    def test_email_contains_readable_audit_attachment(self):
        salvar_json(digest.AUDITORIA_FILE, {"resumo": {"carbono": {"BR": 3}}})
        with patch.object(digest, "EMAIL_DESTINO", "test@example.com"), \
             patch.object(digest.requests, "post", return_value=Mock(status_code=201, json=lambda: {"messageId": "test"})) as post:
            digest.enviar_email("subject", "html")
        attachment = post.call_args.kwargs["json"]["attachment"][0]
        self.assertEqual(attachment["name"], "curadoria.txt")
        self.assertIn("carbono", base64.b64decode(attachment["content"]).decode("utf-8"))
