"""Teste de sanidade fim-a-fim: roda rodar_digest() de ponta a ponta (só a
IA e o envio são simulados) e confirma que a seleção realmente preenche até
3 BR + 1 US por tema, e que uma notícia de mercado REGULADO brasileiro vence
uma vaga mesmo vindo de uma fonte fora da lista de veículos prioritários."""
import time
from unittest.mock import patch

from test_reliability import IsolatedState, client
import digest_email as digest
from reliability import carregar_json


def noticia(titulo, link, fonte, topico):
    return dict(titulo=titulo, resumo=titulo, link=link, aliases=[link], fonte=fonte,
                topico=topico, topicos=[topico], origem="BR", publicado_em=time.time())


def avaliacao(indice, bucket="BR", prioridade=50, fato=None, motivo=None):
    return {"indice": indice, "decisao": "elegivel", "bucket": bucket, "prioridade": prioridade,
            "fato": fato or f"fato-{indice}",
            "motivo": motivo or "Justificativa editorial de teste com contexto suficiente para passar na validação."}


class SanityTests(IsolatedState):
    def test_selection_fills_three_br_one_us_per_topic_and_regulated_market_beats_source_tier(self):
        dc_candidatos = [
            noticia("DC BR 1", "https://reuters.com/dc-br-1", "Reuters", "data_center"),
            noticia("DC BR 2", "https://reuters.com/dc-br-2", "Reuters", "data_center"),
            noticia("DC BR 3", "https://reuters.com/dc-br-3", "Reuters", "data_center"),
            noticia("DC BR 4", "https://reuters.com/dc-br-4", "Reuters", "data_center"),
            noticia("DC BR 5", "https://reuters.com/dc-br-5", "Reuters", "data_center"),
            noticia("DC US 1", "https://reuters.com/dc-us-1", "Reuters", "data_center"),
            noticia("DC US 2", "https://reuters.com/dc-us-2", "Reuters", "data_center"),
        ]
        carbono_candidatos = [
            noticia("Mercado regulado (SBCE) — fonte pequena", "https://portalregionalcarbono.com.br/sbce",
                    "Portal Regional Carbono", "carbono"),
            noticia("Crédito voluntário 1", "https://valor.globo.com/vol-1", "Valor Econômico", "carbono"),
            noticia("Crédito voluntário 2", "https://folha.uol.com.br/vol-2", "Folha de S.Paulo", "carbono"),
            noticia("Crédito voluntário 3", "https://estadao.com.br/vol-3", "Estadão", "carbono"),
            noticia("Carbon market US", "https://reuters.com/carbono-us-1", "Reuters", "carbono"),
        ]

        dc_resposta = client([
            avaliacao(0, "BR", 90), avaliacao(1, "BR", 80), avaliacao(2, "BR", 70),
            avaliacao(3, "BR", 60), avaliacao(4, "BR", 50),
            avaliacao(5, "US", 90), avaliacao(6, "US", 80),
        ]).messages.create.return_value
        carbono_resposta = client([
            # Fonte fora da lista prioritária, mas mercado regulado brasileiro: nota máxima.
            avaliacao(0, "BR", 95, fato="fact-regulado", motivo="Mercado regulado brasileiro (SBCE); prioridade máxima independente da fonte."),
            avaliacao(1, "BR", 60, fato="fact-vol-1"),
            avaliacao(2, "BR", 55, fato="fact-vol-2"),
            avaliacao(3, "BR", 50, fato="fact-vol-3"),
            avaliacao(4, "US", 80, fato="fact-us-carbono"),
        ]).messages.create.return_value

        model = client([])
        model.messages.create.side_effect = [dc_resposta, carbono_resposta]

        with patch.object(digest, "coletar_itens_novos", return_value=dc_candidatos + carbono_candidatos), \
             patch.object(digest.anthropic, "Anthropic", return_value=model), \
             patch.object(digest, "resumir",
                          side_effect=lambda c, itens: [i.update(resumo_final=i["titulo"]) for i in itens]), \
             patch.object(digest, "enviar_email") as send:
            digest.rodar_digest()

        send.assert_called_once()
        auditoria = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(auditoria["resumo"]["data_center"], {"BR": 3, "US": 1})
        self.assertEqual(auditoria["resumo"]["carbono"], {"BR": 3, "US": 1})

        enviados = digest.carregar_estado()
        # Data centers: os 3 BR e o 1 US de maior nota entram; o resto fica de reserva.
        for link in ["https://reuters.com/dc-br-1", "https://reuters.com/dc-br-2",
                     "https://reuters.com/dc-br-3", "https://reuters.com/dc-us-1"]:
            self.assertIn(link, enviados)
        for link in ["https://reuters.com/dc-br-4", "https://reuters.com/dc-br-5", "https://reuters.com/dc-us-2"]:
            self.assertNotIn(link, enviados)

        # Carbono: a notícia de mercado regulado vence mesmo vindo de fonte fora da
        # lista de veículos prioritários; só o voluntário de menor nota fica de fora
        # (4 candidatas BR elegíveis, 3 vagas).
        self.assertIn("https://portalregionalcarbono.com.br/sbce", enviados)
        self.assertIn("https://valor.globo.com/vol-1", enviados)
        self.assertIn("https://folha.uol.com.br/vol-2", enviados)
        self.assertNotIn("https://estadao.com.br/vol-3", enviados)
        self.assertIn("https://reuters.com/carbono-us-1", enviados)
