import base64
import json
from types import SimpleNamespace as NS
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
        # Segundo lote fora do tema: mantém o total de elegíveis em 30, sem disparar o torneio.
        second = client([decision(i, code="fora_tema") for i in range(5)]).messages.create.return_value
        model.messages.create.side_effect = [first, second]
        ranking.classificar(model, "data_center", "", candidates, digest.MODELO)
        self.assertEqual(model.messages.create.call_count, 2)
        self.assertTrue(all("data_center" in c["avaliacoes"] for c in candidates))
        ranking.classificar(model, "data_center", "", candidates, digest.MODELO)
        self.assertEqual(model.messages.create.call_count, 2)  # cache de avaliação

    def test_bad_batch_does_not_abort_other_batches(self):
        candidates = [item(i) for i in range(35)]
        model = client([])
        malformado = NS(stop_reason="end_turn", content=[NS(type="text", text="isso não é JSON")])
        bom = client([decision(i) for i in range(5)]).messages.create.return_value
        model.messages.create.side_effect = [malformado, bom]
        grupos, falhou = ranking.classificar(model, "data_center", "", candidates, digest.MODELO)
        self.assertTrue(falhou)
        self.assertEqual(model.messages.create.call_count, 2)
        self.assertFalse(any("data_center" in c.get("avaliacoes", {}) for c in candidates[:30]))
        self.assertTrue(all("data_center" in c["avaliacoes"] for c in candidates[30:]))
        self.assertEqual(len(grupos["BR"]), 5)

    def test_tournament_round_compares_lot_winners_when_over_thirty(self):
        candidates = [item(i) for i in range(35)]
        lote1 = client([decision(i, score=100 - i) for i in range(30)]).messages.create.return_value
        lote2 = client([decision(i, score=50 - i) for i in range(5)]).messages.create.return_value
        # Só os 15 vencedores (top 10 do 1º lote + os 5 do 2º) avançam para a
        # rodada de comparação direta — com notas propositalmente acima da
        # maior nota de quem ficou de fora (90), pra confirmar que o topo
        # final vem dessa rodada, não da nota original do lote de origem.
        rodada2 = client([decision(i, score=86 + i) for i in range(15)]).messages.create.return_value
        model = client([])
        model.messages.create.side_effect = [lote1, lote2, rodada2]
        grupos, falhou = ranking.classificar(model, "data_center", "", candidates, digest.MODELO)
        self.assertFalse(falhou)
        self.assertEqual(model.messages.create.call_count, 3)
        # ninguém some: os 20 do 1º lote fora do top 10 continuam elegíveis como reserva.
        self.assertEqual(len(grupos["BR"]), 35)
        # a nota que vale pro topo é a da rodada final, não a nota original do lote.
        vencedor = grupos["BR"][0]
        self.assertEqual(vencedor["link"], item(34)["link"])
        self.assertEqual(vencedor["avaliacoes"]["data_center"]["prioridade"], 100)
        # quem ficou de reserva (não entrou na rodada final) mantém a nota original do lote 1.
        reserva = next(c for c in candidates if c["link"] == item(20)["link"])
        self.assertEqual(reserva["avaliacoes"]["data_center"]["prioridade"], 80)

    def test_tournament_winners_that_lose_final_round_are_excluded_but_rest_stay_reserve(self):
        candidates = [item(i) for i in range(31)]
        lote1 = client([decision(i, score=100 - i) for i in range(30)]).messages.create.return_value
        lote2 = client([decision(0, score=50)]).messages.create.return_value
        # Rodada final: o candidato 0 (único vencedor do 2º lote) é reconhecido como
        # duplicata de um dos 10 do 1º lote e descartado nesta comparação direta.
        rodada2 = client([{**decision(i, score=90 - i), "decisao": ("sem_fato_novo" if i == 10 else "elegivel")}
                          for i in range(11)]).messages.create.return_value
        model = client([])
        model.messages.create.side_effect = [lote1, lote2, rodada2]
        grupos, falhou = ranking.classificar(model, "data_center", "", candidates, digest.MODELO)
        self.assertEqual(model.messages.create.call_count, 3)
        # os 20 do 1º lote que nem entraram na rodada final continuam elegíveis (reserva)
        # com a nota original do lote 1 — não desaparecem da fila.
        selecionados = {c["link"] for c in grupos["BR"]}
        self.assertIn(item(20)["link"], selecionados)
        self.assertEqual(next(c for c in candidates if c["link"] == item(20)["link"])
                          ["avaliacoes"]["data_center"]["prioridade"], 80)
        # o item 30 (único do 2º lote) perdeu a rodada final (virou sem_fato_novo)
        self.assertNotIn(item(30)["link"], selecionados)
        self.assertIn(item(0)["link"], selecionados)

    def test_similar_titles_are_treated_as_duplicate_even_with_different_fato(self):
        # Duas coberturas do MESMO acontecimento por veículos diferentes: o modelo, avaliado
        # num único lote aqui, ainda assim erra e devolve "fato" diferente para cada uma —
        # simula o caso real onde itens avaliados em lotes/execuções separadas nunca são
        # comparados entre si pelo modelo. A comparação por título deve pegar isso.
        candidates = [item(0), item(1)]
        candidates[0]["titulo"] = "MPF e DPU pedem paralisação de mega data center do TikTok no Ceará"
        candidates[1]["titulo"] = "MPF e DPU vão à Justiça contra operação de data center do TikTok no Ceará"
        rows = [decision(0, score=90), decision(1, score=80)]
        model = client(rows)
        model.messages.create.side_effect = [model.messages.create.return_value,
            client({"mesmo_fato": True}).messages.create.return_value]
        self.run_model(candidates, model)
        self.assertEqual(len(digest.carregar_estado()), 1)
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        resultados = [n["resultado"] for n in audit["noticias"]]
        self.assertEqual(resultados.count("selecionada"), 1)
        self.assertEqual(resultados.count("duplicada"), 1)
        duplicada = next(n for n in audit["noticias"] if n["resultado"] == "duplicada")
        self.assertIn("parecido", duplicada["motivo"])
        # a de maior prioridade (90) é a escolhida, não a de menor (80).
        self.assertEqual(next(n for n in audit["noticias"] if n["resultado"] == "selecionada")["link"], item(0)["link"])

    def test_similar_title_to_already_sent_item_is_rejected_in_a_later_run(self):
        original = item(0)
        original["titulo"] = "Google investirá 13 bilhões de euros em data centers na Finlândia"
        self.run_model([original], client([decision(0, score=90)]))
        self.assertEqual(digest.carregar_estado(), {original["link"]})

        reimpressao = item(1)
        reimpressao["titulo"] = "Google investirá € 13 bi em data centers na Finlândia, seu maior aporte na Europa"
        linha = {**decision(0, score=90), "fato": "fato-diferente-do-anterior"}
        model = client([linha])
        model.messages.create.side_effect = [model.messages.create.return_value,
            client({"mesmo_fato": True}).messages.create.return_value]
        send = self.run_model([reimpressao], model)
        send.assert_not_called()
        self.assertEqual(digest.carregar_estado(), {original["link"]})
        audit = carregar_json(digest.AUDITORIA_FILE, {})
        registro = next(n for n in audit["noticias"] if n["link"] == reimpressao["link"])
        self.assertEqual(registro["resultado"], "duplicada")
        self.assertIn("parecido", registro["motivo"])

    def test_duplicate_facts_use_reserves_until_quota(self):
        rows = [decision(i, score=100-i) for i in range(5)]
        rows[1]["fato"] = rows[0]["fato"]
        rows[2]["fato"] = rows[0]["fato"]
        model = client(rows)
        model.messages.create.side_effect = [model.messages.create.return_value,
            client({"mesmo_fato": True}).messages.create.return_value,
            client({"mesmo_fato": True}).messages.create.return_value]
        self.run_model([item(i) for i in range(5)], model)
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
        for site in ["agenciainfra.com", "gov.br/mme", "brasilenergia.com.br", "eixos.com.br"]:
            self.assertTrue(fonte_prioritaria({"link": "https://" + site + "/noticia"}))
        self.assertIsNone(fonte_prioritaria({"link": "https://gov.br/outro/noticia"}))
        self.assertNotIn("ri.sanepar.com.br", FONTES)
        self.assertIsNone(fonte_prioritaria({"link": "https://ri.sanepar.com.br/comunicado"}))

    def test_email_contains_readable_audit_attachment(self):
        salvar_json(digest.AUDITORIA_FILE, {"resumo": {"carbono": {"BR": 3}}})
        with patch.object(digest, "EMAIL_DESTINO", "test@example.com"), \
             patch.object(digest.requests, "post", return_value=Mock(status_code=201, json=lambda: {"messageId": "test"})) as post:
            digest.enviar_email("subject", "html")
        attachment = post.call_args.kwargs["json"]["attachment"][0]
        self.assertEqual(attachment["name"], "curadoria.txt")
        self.assertIn("carbono", base64.b64decode(attachment["content"]).decode("utf-8"))
