"""Regressões offline: nenhuma chamada de rede, envio ou alteração de estado real."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import time
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Permite executar no Python portátil sem instalar bibliotecas de rede.
for name, attrs in {
    "requests": dict(get=Mock(), post=Mock(), RequestException=OSError),
    "feedparser": dict(parse=Mock()),
    "googlenewsdecoder": dict(gnewsdecoder=Mock()),
    "anthropic": dict(Anthropic=Mock()),
    "trafilatura": dict(extract=Mock()),
}.items():
    try:
        importlib.import_module(name)
    except ImportError:
        module = ModuleType(name)
        module.__dict__.update(attrs)
        sys.modules[name] = module

import common
import digest_email as digest
import telegram_news_bot as telegram
import persist_state
import resolve_delivery
from reliability import canonica, carregar_json, salvar_json, nivel_fonte


def item(number=0, topics=None):
    topics = topics or ["data_center"]
    link = f"https://reuters.com/article/{number}"
    return dict(titulo=f"Data center mercado de carbono {number}", resumo="Texto do feed",
                link=link, aliases=[link], fonte="Reuters", topico=topics[0],
                topicos=topics, origem="BR", publicado_em=time.time())


def client(data, stop="end_turn"):
    return NS(messages=NS(create=Mock(return_value=NS(
        stop_reason=stop, content=[NS(type="text", text=json.dumps(data))]))))


class IsolatedState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for module, attr in [(digest, "ESTADO_FILE"), (digest, "FILA_FILE"),
                             (digest, "AUDITORIA_FILE"),
                             (digest, "INCERTO_FILE"), (telegram, "SENT_FILE"),
                             (telegram, "INCERTO_FILE"), (common, "_CACHE_GOOGLE_FILE")]:
            p = patch.object(module, attr, str(Path(self.temp.name) / (module.__name__ + attr + ".json")))
            p.start()
            self.addCleanup(p.stop)
        for module, attr, value in [(digest, "checkpoint", Mock()), (telegram, "checkpoint", Mock()),
                                    (common, "_cache_google", {}), (telegram.time, "sleep", Mock())]:
            p = patch.object(module, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def run_digest(self, candidates, selection):
        with patch.object(digest, "coletar_itens_novos", return_value=candidates), \
             patch.object(digest.anthropic, "Anthropic", return_value=client([])), \
             patch.object(digest, "selecionar", side_effect=selection), \
             patch.object(digest, "resumir"), patch.object(digest, "montar_html", return_value="html"), \
             patch.object(digest, "enviar_email") as send:
            digest.rodar_digest()
            return send


class RegressionTests(IsolatedState):
    def test_pending_over_cap_is_not_sent_or_lost(self):
        candidates = [item(i) for i in range(401)]
        batches = []
        def choose(c, t, batch):
            batches.append(batch)
            return {"BR": [batch[0]], "US": []}
        self.run_digest(candidates, choose)
        state = digest.carregar_estado()
        queue = carregar_json(digest.FILA_FILE, {})
        self.assertEqual(state, {item(0)["link"]})
        self.assertEqual(queue[item(400)["link"]]["status"], "pendente")
        self.assertIn("data_center", queue[item(400)["link"]]["avaliado"])
        self.run_digest([], choose)  # já saiu do feed, mas continua na fila
        self.assertIn(item(400)["link"], [i["link"] for i in batches[1]])

    def test_empty_selection_rejects_without_fallback(self):
        # Um array vazio agora é incompleto: toda rejeição precisa de justificativa.
        with self.assertRaises(RuntimeError):
            digest.selecionar(client([]), "data_center", [item()])
        send = self.run_digest([item()], lambda *a: {"BR": [], "US": []})
        send.assert_not_called()
        self.assertEqual(carregar_json(digest.FILA_FILE, {})[item()["link"]]["status"], "pendente")
        self.assertEqual(digest.carregar_estado(), set())

    def test_api_failure_keeps_queue_pending(self):
        with self.assertRaises(RuntimeError):
            self.run_digest([item()], Mock(side_effect=RuntimeError("API falhou")))
        self.assertEqual(carregar_json(digest.FILA_FILE, {})[item()["link"]]["status"], "pendente")
        self.assertEqual(digest.carregar_estado(), set())

    def test_duplicate_or_invalid_indices_reject_entire_response(self):
        bad = [[{"indice": 0, "bucket": "BR"}] * 2,
               [{"indice": 0, "bucket": "BR"}, {"indice": 0, "bucket": "US"}],
               [{"indice": True, "bucket": "BR"}], [{"indice": 9, "bucket": "BR"}],
               [{"indice": 0, "bucket": []}], {}, [None]]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                digest.selecionar(client(data), "data_center", [item()])
        with self.assertRaises(RuntimeError):
            digest.selecionar(client([], "max_tokens"), "data_center", [item()])

    def test_summary_invalid_types_fall_back_to_feed(self):
        for data in [[{"indice": 0, "resumo": 123}], [{"indice": True, "resumo": "falso"}],
                     [{"indice": 0, "resumo": "A"}, {"indice": 0, "resumo": "B"}], {}]:
            candidates = [item()]
            with patch.object(digest, "buscar_texto_artigo", return_value="Texto de apoio com informação. " * 10):
                digest.resumir(client(data), candidates)
            self.assertEqual(candidates[0]["resumo_final"], "Texto do feed")

    def test_collect_deduplicates_google_and_preserves_topics(self):
        google = "https://news.google.com/rss/articles/abc"
        topics = {t: dict(keywords=["data center"], feeds=[dict(url=t, origem="BR")])
                  for t in ("data_center", "carbono")}
        feed = NS(bozo=False, feed={}, entries=[
            dict(link=google, title="data center", source={"title": "Reuters"}),
            dict(link=item()["link"] + "?utm_source=rss", title="data center")])
        with patch.object(common, "TOPICOS", topics), patch.object(common, "_parse_feed", return_value=feed), \
             patch.object(common, "gnewsdecoder", return_value={"status": True, "decoded_url": item()["link"]}):
            collected = common.coletar_itens_novos(set(), resolver=True)
            self.assertEqual(len(collected), 1)
            self.assertEqual(set(collected[0]["topicos"]), {"data_center", "carbono"})
            self.assertIn(google, collected[0]["aliases"])
            self.assertEqual(common.coletar_itens_novos({google}), [])

    def test_second_topic_can_select_first_topics_rejection(self):
        def choose(c, topic, batch):
            return {"BR": [] if topic == "data_center" else [batch[0]], "US": []}
        send = self.run_digest([item(topics=["data_center", "carbono"])], choose)
        send.assert_called_once()
        self.assertEqual(digest.carregar_estado(), {item()["link"]})

    def test_canonical_and_source_policy(self):
        self.assertEqual(canonica("http://www.reuters.com/a?utm_source=rss&x=1#top"), "https://reuters.com/a?x=1")
        for url in ["https://reuters.com.evil.test/a", "https://evil.test/a", "https://fake.reuters.com/a",
                    "javascript:alert(1)", "https://reuters.com@evil.test/a"]:
            self.assertEqual(nivel_fonte(dict(link=url, fonte="Reuters Fake Releases")), 99)
        self.assertEqual(nivel_fonte(item()), 1)

    def test_telegram_partial_success_timeout_and_restart(self):
        with patch.object(telegram, "coletar_itens_novos", return_value=[item(1), item(2)]), \
             patch.object(telegram, "enviar_telegram", side_effect=[True, telegram.requests.RequestException("timeout")]):
            with self.assertRaises(RuntimeError):
                telegram.checar_feeds()
        self.assertEqual(telegram.carregar_enviados(), {item(1)["link"]})
        self.assertEqual(carregar_json(telegram.INCERTO_FILE, {})["aliases"], [item(2)["link"]])
        with patch.object(telegram, "enviar_telegram") as send, self.assertRaises(RuntimeError):
            telegram.checar_feeds()
        send.assert_not_called()

    def test_no_send_if_preflight_persistence_fails(self):
        telegram.checkpoint.side_effect = RuntimeError("push falhou")
        with patch.object(telegram, "coletar_itens_novos", return_value=[item()]), \
             patch.object(telegram, "enviar_telegram") as send, self.assertRaises(RuntimeError):
            telegram.checar_feeds()
        send.assert_not_called()

    def test_telegram_rate_limit_uses_retry_after(self):
        response = lambda code, data: NS(status_code=code, json=lambda: data)
        with patch.object(telegram.requests, "post", side_effect=[
            response(429, {"ok": False, "parameters": {"retry_after": 8}}),
            response(200, {"ok": True, "result": {"message_id": 1}})]):
            self.assertTrue(telegram.enviar_telegram("titulo", item()["link"], "Reuters"))
        telegram.time.sleep.assert_called_once_with(8)

    def test_email_timeout_blocks_repeat(self):
        salvar_json(digest.INCERTO_FILE, {"aliases": [item()["link"]]})
        with patch.object(digest, "enviar_email") as send, self.assertRaises(RuntimeError):
            digest.rodar_digest()
        send.assert_not_called()

    def test_email_preflight_failure_prevents_post(self):
        digest.checkpoint.side_effect = RuntimeError("push falhou")
        with patch.object(digest, "coletar_itens_novos", return_value=[item()]), \
             patch.object(digest.anthropic, "Anthropic", return_value=client([])), \
             patch.object(digest, "selecionar", return_value={"BR": [item()], "US": []}), \
             patch.object(digest, "resumir"), patch.object(digest, "montar_html", return_value="html"), \
             patch.object(digest, "enviar_email") as send, self.assertRaises(RuntimeError):
            digest.rodar_digest()
        send.assert_not_called()
        self.assertTrue(carregar_json(digest.INCERTO_FILE, None))
        self.assertEqual(digest.carregar_estado(), set())

    def test_brevo_rejection_allows_retry_but_5xx_remains_uncertain(self):
        for code in (400, 429, 500):
            salvar_json(digest.INCERTO_FILE, {"aliases": [item()["link"]]})
            with patch.object(digest, "EMAIL_DESTINO", "test@example.com"), \
                 patch.object(digest.requests, "post", return_value=NS(status_code=code)), \
                 self.assertRaises(RuntimeError):
                digest.enviar_email("teste", "html")
            self.assertEqual(bool(carregar_json(digest.INCERTO_FILE, None)), code == 500)

    def test_expired_candidate_does_not_reenter(self):
        candidates = [item()]
        self.run_digest(candidates, lambda *a: {"BR": [item()], "US": []})
        queue = carregar_json(digest.FILA_FILE, {})
        queue[item()["link"]].update(status="pendente", criado=0)
        salvar_json(digest.FILA_FILE, queue)
        salvar_json(digest.ESTADO_FILE, [])
        choose = Mock()
        self.run_digest(candidates, choose)
        choose.assert_not_called()
        self.assertEqual(carregar_json(digest.FILA_FILE, {})[item()["link"]]["status"], "expirado")

    def test_manual_resolution_marks_only_confirmed_delivery(self):
        root = Path(self.temp.name)
        for delivered in (False, True):
            salvar_json(root / "telegram_incerto.json", {"aliases": [item()["link"]]})
            with patch.object(resolve_delivery, "ROOT", root), patch.object(resolve_delivery, "checkpoint"):
                resolve_delivery.resolver("telegram", delivered)
            self.assertIsNone(carregar_json(root / "telegram_incerto.json", {}))
            self.assertEqual(carregar_json(root / "enviados.json", []), [item()["link"]] if delivered else [])

    def test_actual_git_checkpoint_pushes_state_only(self):
        root = Path(self.temp.name)
        remote, checkout = root / "remote.git", root / "checkout"
        def run(*args, cwd=None):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout
        run("init", "--bare", str(remote))
        run("init", "-b", "main", str(checkout))
        run("config", "user.name", "Test", cwd=checkout)
        run("config", "user.email", "test@example.com", cwd=checkout)
        (checkout / "app.py").write_text("# initial\n")
        run("add", "app.py", cwd=checkout)
        run("commit", "-m", "initial", cwd=checkout)
        run("remote", "add", "origin", str(remote), cwd=checkout)
        run("push", "-u", "origin", "main", cwd=checkout)
        salvar_json(checkout / "enviados.json", [item()["link"]])
        (checkout / "unrelated.txt").write_text("untracked")
        with patch.object(persist_state, "ROOT", checkout), patch.dict(os.environ, {"BOT_BRANCH": "main"}):
            persist_state.persistir()
        actual = run("--git-dir", str(remote), "show", "main:enviados.json")
        self.assertEqual(json.loads(actual), [item()["link"]])
        self.assertNotIn("unrelated.txt", run("--git-dir", str(remote), "ls-tree", "--name-only", "main"))

    def test_git_retries_failed_push_without_resending(self):
        calls = []
        pushes = iter([1, 0])
        def git(*args, **kwargs):
            calls.append(args)
            return NS(returncode=next(pushes) if args[0] == "push" else 0)
        with patch.object(persist_state, "git", side_effect=git):
            persist_state.persistir()
        self.assertEqual(sum(c[0] == "push" for c in calls), 2)

    def test_workflows_share_lock_and_refresh_branch(self):
        for file in ["news.yml", "digest.yml"]:
            text = (ROOT / ".github" / "workflows" / file).read_text(encoding="utf-8")
            self.assertIn("group: news-bot-state-${{ github.ref }}", text)
            self.assertIn("ref: ${{ github.ref_name }}", text)
            self.assertIn("if: ${{ always() }}", text)
            self.assertIn('BOT_PERSIST_STATE: "1"', text)


if __name__ == "__main__":
    unittest.main()
