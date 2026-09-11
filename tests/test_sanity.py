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
    def test_battery_topic_fills_two_brazil_and_two_international_slots(self):
        candidatos = [
            noticia("Aneel define preço-teto do leilão de baterias marcado para 2027",
                    "https://megawhat.uol.com.br/bat-br-1", "MegaWhat", "baterias"),
            noticia("BNDES prevê R$ 34 bilhões em financiamento para armazenamento",
                    "https://valor.globo.com/bat-br-2", "Valor Econômico", "baterias"),
            noticia("Custo de sistemas de armazenamento cai 18% no mercado nacional",
                    "https://eixos.com.br/bat-br-3", "Agência eixos", "baterias"),
            noticia("CATL cuts cell prices as the global battery war intensifies",
                    "https://ft.com/bat-us-1", "Financial Times", "baterias"),
            noticia("Panasonic solid-state cell reaches new energy density mark",
                    "https://reuters.com/bat-us-2", "Reuters", "baterias"),
            noticia("BYD expands blade battery output at its Hungary plant",
                    "https://bloomberg.com/bat-us-3", "Bloomberg", "baterias"),
        ]
        resposta = client([
            avaliacao(0, "BR", 95, fato="leilao-aneel"), avaliacao(1, "BR", 85, fato="bndes"),
            avaliacao(2, "BR", 70, fato="custo-br"),
            avaliacao(3, "US", 95, fato="catl-preco"), avaliacao(4, "US", 85, fato="solid-state"),
            avaliacao(5, "US", 70, fato="byd-hungria"),
        ]).messages.create.return_value
        model = client([])
        model.messages.create.side_effect = [resposta]

        with patch.object(digest, "coletar_itens_novos", return_value=candidatos), \
             patch.object(digest.anthropic, "Anthropic", return_value=model), \
             patch.object(digest, "resumir",
                          side_effect=lambda c, itens: [i.update(resumo_final=i["titulo"]) for i in itens]), \
             patch.object(digest, "enviar_email") as send:
            digest.rodar_digest()

        send.assert_called_once()
        auditoria = carregar_json(digest.AUDITORIA_FILE, {})
        self.assertEqual(auditoria["resumo"]["baterias"], {"BR": 2, "US": 2})
        enviados = digest.carregar_estado()
        for link in ["https://megawhat.uol.com.br/bat-br-1", "https://valor.globo.com/bat-br-2",
                     "https://ft.com/bat-us-1", "https://reuters.com/bat-us-2"]:
            self.assertIn(link, enviados)
        for link in ["https://eixos.com.br/bat-br-3", "https://bloomberg.com/bat-us-3"]:
            self.assertNotIn(link, enviados)

    def test_email_sections_follow_data_center_batteries_carbon_order(self):
        selecao = {t: {"BR": [dict(titulo=f"Manchete {t}", fonte="Reuters", resumo_final="Resumo.",
                                   link=f"https://reuters.com/{t}")], "US": []}
                   for t in ("data_center", "baterias", "carbono")}
        html = digest.montar_html(selecao, "11/09/2026 · manhã")
        # O rótulo seguido de </span> é a faixa da seção; o <h1> do cabeçalho cita os três temas.
        posicoes = [html.index(digest.TOPICOS[t]["rotulo"] + "</span>")
                    for t in ("data_center", "baterias", "carbono")]
        self.assertEqual(posicoes, sorted(posicoes))
        self.assertIn("#b45309", html)  # selo do tema de baterias


    def test_selection_fills_three_br_one_us_per_topic_and_regulated_market_beats_source_tier(self):
        dc_candidatos = [
            noticia("Data center recebe aval para conexão à rede básica em Campinas", "https://reuters.com/dc-br-1", "Reuters", "data_center"),
            noticia("Governo estadual anuncia incentivo fiscal para novos data centers", "https://reuters.com/dc-br-2", "Reuters", "data_center"),
            noticia("Operadora investe em subestação dedicada para atender data center", "https://reuters.com/dc-br-3", "Reuters", "data_center"),
            noticia("Consórcio vence leilão de energia para suprir data center no Sul", "https://reuters.com/dc-br-4", "Reuters", "data_center"),
            noticia("Justiça suspende licenciamento de data center após recurso do MP", "https://reuters.com/dc-br-5", "Reuters", "data_center"),
            noticia("Fabricante anuncia expansão de capacidade de data centers na Ásia", "https://reuters.com/dc-us-1", "Reuters", "data_center"),
            noticia("Regulador americano revisa tarifa de energia para data centers", "https://reuters.com/dc-us-2", "Reuters", "data_center"),
        ]
        carbono_candidatos = [
            noticia("Mercado regulado (SBCE) — fonte pequena exige precificação de emissões", "https://portalregionalcarbono.com.br/sbce",
                    "Portal Regional Carbono", "carbono"),
            noticia("Fundo capta recursos para créditos de carbono florestal no Pará", "https://valor.globo.com/vol-1", "Valor Econômico", "carbono"),
            noticia("Empresa de logística compensa emissões com créditos voluntários", "https://folha.uol.com.br/vol-2", "Folha de S.Paulo", "carbono"),
            noticia("Cooperativa agrícola negocia certificação de crédito de carbono", "https://estadao.com.br/vol-3", "Estadão", "carbono"),
            noticia("Regulador europeu debate reforma do mercado de carbono", "https://reuters.com/carbono-us-1", "Reuters", "carbono"),
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
