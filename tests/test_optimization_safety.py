import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch
from test_reliability import IsolatedState, item, client
from test_ranking import decision
import digest_email as digest
import email_ranking as ranking
import email_usage as usage
from email_exact_duplicates import unificar
from reliability import salvar_json
from email_dedup import comparador


class RoundCacheTests(TestCase):
    def test_repeated_tournament_matches_first_result_without_new_calls(self):
        candidates = [item(i) for i in range(35)]
        def respond(**kwargs):
            # Cada resposta cobre o tamanho do lote: 30, 5, 15 finalistas.
            prompt = kwargs['messages'][0]['content']
            start = prompt.index('[{"indice":')
            data, _ = json.JSONDecoder().raw_decode(prompt[start:])
            return client([decision(i, score=i) for i in range(len(data))]).messages.create.return_value
        model = client([])
        model.messages.create.side_effect = respond
        first, failed = ranking.classificar(model, 'data_center', '', candidates, 'modelo')
        self.assertFalse(failed)
        expected = [c['link'] for c in first['BR']]
        calls = model.messages.create.call_count
        second, failed = ranking.classificar(model, 'data_center', '', candidates, 'modelo')
        self.assertFalse(failed)
        self.assertEqual(expected, [c['link'] for c in second['BR']])
        self.assertEqual(calls, model.messages.create.call_count)
        # Uma notícia modificada exige classificação e novo confronto.
        candidates[0]['resumo'] += ' Nova informação substantiva.'
        ranking.classificar(model, 'data_center', '', candidates, 'modelo')
        self.assertGreater(model.messages.create.call_count, calls)

    def test_old_base_cache_still_works_and_editorial_change_invalidates(self):
        candidate = item()
        signature = hashlib.sha256(json.dumps([ranking.VERSAO, ranking.PROMPT, 'data_center', '', 'modelo',
            {k: candidate.get(k) for k in ('titulo', 'fonte', 'link', 'resumo')}],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        candidate['cache_avaliacoes'] = {'data_center': {'assinatura': signature, 'avaliacao': decision(0)}}
        model = client([decision(0)])
        ranking.classificar(model, 'data_center', '', [candidate], 'modelo')
        model.messages.create.assert_not_called()
        ranking.classificar(model, 'data_center', 'Novo foco editorial', [candidate], 'modelo')
        model.messages.create.assert_called_once()

    def test_failed_round_is_not_cached(self):
        candidate = item()
        candidate.update(_assinatura_ranking='base', avaliacoes={'data_center': decision(0)})
        model = client([])
        model.messages.create.side_effect = TimeoutError()
        self.assertFalse(ranking.avaliar_rodada(model, 'data_center', '', [candidate], 'modelo', 1, []))
        self.assertFalse(candidate['cache_rodadas']['data_center'])


class ExactDuplicateTests(TestCase):
    def pair(self):
        a = item()
        b = copy.deepcopy(a)
        b['link'] = 'https://news.google.com/rss/articles/abc'
        b['aliases'] = [b['link']]
        return [{'item': a}, {'item': b}]

    def test_resolved_identical_article_merged_and_aliases_preserved(self):
        records = self.pair()
        kept, copies = unificar(records, lambda _: records[0]['item']['link'])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(copies), 1)
        self.assertIn('https://news.google.com/rss/articles/abc', kept[0]['item']['aliases'])

    def test_same_headline_different_urls_kept(self):
        records = self.pair()
        kept, _ = unificar(records, lambda _: 'https://another.example/article')
        self.assertEqual(len(kept), 2)

    def test_resolution_failure_keeps_both(self):
        records = self.pair()
        kept, _ = unificar(records, Mock(side_effect=TimeoutError()))
        self.assertEqual(len(kept), 2)

    def test_different_date_or_content_or_headline_kept(self):
        for field, value in [('publicado_em', 1), ('resumo', 'Outra informação com novo valor de investimento'),
                             ('titulo', 'Novo desdobramento sobre data centers')]:
            records = self.pair()
            records[1]['item'][field] = value
            kept, _ = unificar(records, lambda _: records[0]['item']['link'])
            self.assertEqual(len(kept), 2, field)


class UsageTests(TestCase):
    def setUp(self):
        usage.iniciar()

    def test_counts_returned_usage_even_if_response_is_truncated(self):
        model = client([])
        response = model.messages.create.return_value
        response.usage = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        response.stop_reason = 'max_tokens'
        self.assertIs(usage.criar_mensagem(model, 'ranking', 'baterias', model='claude-haiku-4-5'), response)
        report = usage.relatorio()
        self.assertEqual(report['custo_estimado_usd'], .0015)
        self.assertEqual(report['chamadas'][0]['topico'], 'baterias')

    def test_missing_usage_and_errors_are_unknown_not_free(self):
        model = client([])
        usage.criar_mensagem(model, 'resumos', model='claude-haiku-4-5')
        model.messages.create.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            usage.criar_mensagem(model, 'ranking', model='claude-haiku-4-5')
        self.assertEqual(usage.relatorio()['chamadas_sem_custo_medido'], 2)

    def test_new_run_resets_usage(self):
        usage.criar_mensagem(client([]), 'resumos', model='modelo')
        usage.iniciar()
        self.assertEqual(usage.relatorio()['chamadas'], [])


class ComparisonReuseTests(TestCase):
    def test_valid_comparison_reused_between_topics(self):
        shared = {}
        model = client({'mesmo_fato': False})
        a, b = 'Projeto A de data center', 'Projeto B de data center'
        self.assertFalse(comparador(model, 'modelo', 'data_center', shared)(a, b, forcar=True))
        self.assertFalse(comparador(model, 'modelo', 'baterias', shared)(b, a, forcar=True))
        model.messages.create.assert_called_once()
        # Outro texto ou modelo exige decisão independente.
        comparador(model, 'modelo', 'data_center', shared)(a, b + ' ampliado', forcar=True)
        comparador(model, 'outro-modelo', 'data_center', shared)(a, b, forcar=True)
        self.assertEqual(model.messages.create.call_count, 3)

    def test_failure_is_not_shared_as_valid_negative(self):
        shared = {}
        model = client({'mesmo_fato': True})
        response = model.messages.create.return_value
        model.messages.create.side_effect = [TimeoutError(), response]
        self.assertFalse(comparador(model, 'modelo', 'data_center', shared)('A', 'B', forcar=True))
        self.assertEqual(shared, {})
        self.assertTrue(comparador(model, 'modelo', 'baterias', shared)('A', 'B', forcar=True))
        self.assertEqual(model.messages.create.call_count, 2)

    def test_new_run_cache_requires_new_comparison(self):
        model = client({'mesmo_fato': False})
        for shared in ({}, {}):
            comparador(model, 'modelo', 'data_center', shared)('A', 'B', forcar=True)
        self.assertEqual(model.messages.create.call_count, 2)


class PreviewTests(IsolatedState):
    def test_legacy_preview_uses_only_confirmed_saved_items(self):
        candidate = item()
        candidate['resumo_final'] = 'Resumo preservado'
        salvar_json(digest.FILA_FILE, {'a': {'item': candidate, 'status': 'enviado'}})
        salvar_json(digest.AUDITORIA_FILE, {'gerado_em': 1789309674, 'noticias': [
            {'resultado': 'selecionada', 'topico': 'data_center', 'titulo': candidate['titulo'],
             'link': candidate['link'], 'avaliacao': {'bucket': 'BR'}}]})
        path = Path(self.temp.name) / 'legacy.html'
        digest.gerar_previa(path)
        self.assertIn('Resumo preservado', path.read_text(encoding='utf-8'))
        salvar_json(digest.FILA_FILE, {'a': {'item': candidate, 'status': 'pendente'}})
        with self.assertRaises(ValueError):
            digest.gerar_previa(Path(self.temp.name) / 'incomplete.html')

    def test_preview_is_same_html_without_api_send_or_state_changes(self):
        candidate = item()
        candidate['resumo_final'] = 'Resumo de teste <seguro>'
        selection = {'data_center': {'BR': [candidate], 'US': []}}
        digest.salvar_ultima_edicao(selection, 'Teste')
        path = Path(self.temp.name) / 'preview.html'
        before = Path(digest.arquivo_ultima_edicao()).read_bytes()
        with patch.object(digest.anthropic, 'Anthropic') as api, patch.object(digest, 'enviar_email') as send, \
             patch.object(digest, 'coletar_itens_novos') as collect:
            digest.gerar_previa(path)
            api.assert_not_called()
            send.assert_not_called()
            collect.assert_not_called()
        self.assertEqual(path.read_text(encoding='utf-8'), digest.montar_html(selection, 'Teste'))
        self.assertEqual(Path(digest.arquivo_ultima_edicao()).read_bytes(), before)

    def test_missing_snapshot_does_not_start_live_run(self):
        with self.assertRaises(ValueError):
            digest.gerar_previa(Path(self.temp.name) / 'preview.html')
