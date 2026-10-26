"""fake 裏方でサーバを立て、OpenAI 形式の往復を確かめる。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as srv  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.fake import FakeWorker, default_responder  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


class ServerTestCase(unittest.TestCase):
    windows = 2

    log_dir = ""

    @classmethod
    def setUpClass(cls):
        cls.cfg = Config(backend="fake", windows=cls.windows, port=0, log_dir=cls.log_dir)
        cls.bridge = srv.Bridge(cls.cfg)
        cls.bridge.start()
        cls.httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(cls.bridge))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        FakeWorker.responder = staticmethod(default_responder)

    def post(self, body, raw=False):
        req = urllib.request.Request(self.base + "/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read().decode()
        return data if raw else json.loads(data)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())


class ChatTests(ServerTestCase):
    def test_models_and_health(self):
        self.assertTrue(self.get("/health")["ok"])
        self.assertEqual(self.get("/v1/models")["data"][0]["id"], "copilot")

    def test_tool_call_round_trip(self):
        seen = []

        def responder(text, worker):
            seen.append(text)
            if "<tool_result name=" in text:
                return "【最終回答】読みました。中身は hello です。"
            return 'ファイルを読みます。\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n</tool_call>'

        FakeWorker.responder = staticmethod(responder)
        msgs = [{"role": "system", "content": "you are an agent (tc)"},
                {"role": "user", "content": "a.txt を読んで"}]
        r1 = self.post({"model": "copilot", "messages": msgs, "tools": TOOLS})
        m1 = r1["choices"][0]["message"]
        self.assertEqual(r1["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(m1["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(m1["tool_calls"][0]["function"]["arguments"]), {"path": "a.txt"})
        self.assertIn("【オペレーターが実行できる操作】", seen[0])
        self.assertIn("read_file", seen[0])

        msgs2 = msgs + [m1, {"role": "tool", "tool_call_id": m1["tool_calls"][0]["id"],
                             "name": "read_file", "content": "hello"}]
        r2 = self.post({"model": "copilot", "messages": msgs2, "tools": TOOLS})
        self.assertEqual(r2["choices"][0]["finish_reason"], "stop")
        self.assertIn("hello", r2["choices"][0]["message"]["content"])
        # 2 回目は差分（ツール結果）だけが送られ、規則は再送されない
        self.assertIn('<tool_result name="read_file">', seen[1])
        self.assertNotIn("【オペレーターが実行できる操作】", seen[1])

        st = self.get("/status")
        sess = [s for s in st["sessions"] if s["turns"] == 2]
        self.assertEqual(len(sess), 1)
        wid = sess[0]["worker"]
        w = next(w for w in st["windows"] if w["id"] == wid)
        self.assertEqual(w["chats"], w["chats"])  # 存在確認

    def test_stream(self):
        FakeWorker.responder = staticmethod(lambda text, w: "流れる回答")
        raw = self.post({"model": "copilot", "stream": True,
                         "messages": [{"role": "user", "content": "stream test"}]}, raw=True)
        events = [json.loads(line[6:]) for line in raw.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        self.assertTrue(raw.strip().endswith("data: [DONE]"))
        content = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
        self.assertEqual(content, "流れる回答")
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")

    def test_bad_request(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post({"model": "copilot", "messages": []})
        self.assertEqual(cm.exception.code, 400)

    def test_parallel_sessions_use_different_windows(self):
        FakeWorker.responder = staticmethod(lambda text, w: f"from {w.id}")
        FakeWorker.delay_sec = 0.3
        results = {}

        def run(name):
            r = self.post({"model": "copilot", "messages": [
                {"role": "system", "content": f"agent {name}"}, {"role": "user", "content": "go"}]})
            results[name] = r["choices"][0]["message"]["content"]

        ts = [threading.Thread(target=run, args=(n,)) for n in ("p1", "p2")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        FakeWorker.delay_sec = 0.0
        self.assertEqual(sorted(results.values()), ["from w0", "from w1"])


class RestartTests(ServerTestCase):
    windows = 1

    def test_worker_restart_retries_once_with_fresh_chat(self):
        from bridge.pool import WorkerRestarted

        seen = []
        state = {"raised": False}

        def responder(text, worker):
            seen.append(text)
            if not state["raised"]:
                state["raised"] = True
                raise WorkerRestarted("w0: ブラウザを再起動しました（not connected to DevTools）")
            return "復帰後の回答"

        FakeWorker.responder = staticmethod(responder)
        r = self.post({"messages": [{"role": "system", "content": "R"}, {"role": "user", "content": "r1"}]})
        self.assertEqual(r["choices"][0]["message"]["content"], "復帰後の回答")
        self.assertEqual(len(seen), 2)
        self.assertIn("優先です。\nR\n", seen[1])   # 会話を作り直して送り直している
        w = self.get("/status")["windows"][0]
        self.assertEqual(w["chats"], 2)


class NudgeTests(ServerTestCase):
    windows = 1

    def test_refusal_is_nudged_into_tool_call(self):
        seen = []

        def responder(text, worker):
            seen.append(text)
            if len(seen) == 1:
                return "作業対象を確認しましたが、現在アクセス可能な作業領域 /mnt/data に src は存在しませんでした。"
            return '<tool_call>\n{"name": "read_file", "arguments": {"path": "src/util.py"}}\n</tool_call>'

        FakeWorker.responder = staticmethod(responder)
        r = self.post({"messages": [{"role": "system", "content": "N"}, {"role": "user", "content": "src を読んで"}],
                       "tools": TOOLS})
        self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(len(seen), 2)
        self.assertIn("オペレーターへの指示の書式", seen[1])   # 言い直しが送られた
        self.assertIn("【回答の例】", seen[0])
        self.assertIn('"name": "read_file"', seen[0])     # 例は実際のツール名

    def test_no_nudge_without_tools(self):
        seen = []
        FakeWorker.responder = staticmethod(lambda text, w: (seen.append(text), "ファイルは存在しません")[1])
        r = self.post({"messages": [{"role": "user", "content": "nudge なし"}]})
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len(seen), 1)


class TranscriptTests(ServerTestCase):
    windows = 1
    import tempfile as _tf
    log_dir = _tf.mkdtemp(prefix="copilot-bridge-logs-")

    def test_session_transcript_written(self):
        from pathlib import Path

        FakeWorker.responder = staticmethod(lambda text, w: "記録される回答")
        self.post({"messages": [{"role": "user", "content": "記録試験"}]})
        files = list((Path(self.log_dir) / "sessions").glob("*.md"))
        self.assertEqual(len(files), 1)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("### 送信", body)
        self.assertIn("記録試験", body)
        self.assertIn("記録される回答", body)
        self.assertTrue(list(Path(self.log_dir).glob("*.jsonl")))


class ValidationTests(ServerTestCase):
    windows = 1

    def test_unknown_tool_name_is_nudged(self):
        seen = []

        def responder(text, worker):
            seen.append(text)
            if len(seen) == 1:
                return '<tool_call>\n{"name": "open_file", "arguments": {"path": "a"}}\n</tool_call>'
            return '<tool_call>\n{"name": "read_file", "arguments": {"path": "a"}}\n</tool_call>'

        FakeWorker.responder = staticmethod(responder)
        r = self.post({"messages": [{"role": "user", "content": "v1"}], "tools": TOOLS})
        self.assertEqual(r["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertIn("open_file", seen[1])   # 言い直しに不正な名前を含める

    def test_only_invalid_calls_is_retryable_error(self):
        FakeWorker.responder = staticmethod(
            lambda text, w: '<tool_call>\n{"name": "nope", "arguments": {}}\n</tool_call>')
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post({"messages": [{"role": "user", "content": "v2"}], "tools": TOOLS})
        self.assertEqual(cm.exception.code, 503)

    def test_conversation_id_header_links_sessions(self):
        seen = []
        FakeWorker.responder = staticmethod(lambda text, w: (seen.append(text), "ok")[1])

        def post(msgs, key):
            req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps({"messages": msgs}).encode(),
                                         headers={"Content-Type": "application/json", "X-Conversation-Id": key})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())

        a = [{"role": "user", "content": "h1"}]
        r1 = post(a, "run-1/pm")
        post(a + [r1["choices"][0]["message"], {"role": "user", "content": "h2"}], "run-1/pm")
        self.assertNotIn("【書式】", seen[1])       # ID で続きと分かり、差分だけ送る
        # 履歴が書き換わった（要約など）ら同じ ID でも作り直す
        post([{"role": "user", "content": "rewritten"}], "run-1/pm")
        self.assertIn("【書式】", seen[2])
        st = self.get("/status")
        self.assertTrue(any(s["key"] == "run-1/pm" for s in st["sessions"]))


class FinalMarkTests(ServerTestCase):
    windows = 1

    def test_plain_chat_without_mark_is_nudged_then_accepted(self):
        seen = []

        def responder(text, worker):
            seen.append(text)
            if len(seen) == 1:
                return "はい、承知しました。ファイルを確認して進めますね。"   # 普通の受け答え（書式違反）
            return "【最終回答】作業は不要です。"

        FakeWorker.responder = staticmethod(responder)
        r = self.post({"messages": [{"role": "user", "content": "fm1"}], "tools": TOOLS})
        self.assertEqual(len(seen), 2)
        self.assertIn("書式が守られていません", seen[1])
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")
        self.assertEqual(r["choices"][0]["message"]["content"], "作業は不要です。")   # 印は除く
        self.assertIn("【念押し】", seen[0])
        self.assertIn("【元の指示（参考情報）】", seen[0])

    def test_no_tools_plain_chat_is_not_nudged(self):
        seen = []
        FakeWorker.responder = staticmethod(lambda text, w: (seen.append(text), "普通の答え")[1])
        r = self.post({"messages": [{"role": "user", "content": "fm2"}]})
        self.assertEqual(len(seen), 1)
        self.assertEqual(r["choices"][0]["message"]["content"], "普通の答え")


class ModelSelectTests(ServerTestCase):
    windows = 1

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cfg.models = {"gpt": "GPT-5", "opus": "Claude Opus"}

    def test_models_listed_and_label_passed_to_new_chat(self):
        ids = [m["id"] for m in self.get("/v1/models")["data"]]
        self.assertEqual(ids, ["copilot", "gpt", "opus"])
        FakeWorker.responder = staticmethod(lambda text, w: "【最終回答】ok")
        self.post({"model": "opus", "messages": [{"role": "user", "content": "ms1"}]})
        self.post({"model": "Claude Opus", "messages": [{"role": "user", "content": "ms2"}]})
        self.post({"model": "copilot", "messages": [{"role": "user", "content": "ms3"}]})
        w = self.bridge.pool.workers[0]
        self.assertEqual(w.models_selected[-3:], ["Claude Opus", "Claude Opus", None])


class EvictionTests(ServerTestCase):
    windows = 1

    def test_evicted_session_resends_full_history(self):
        seen = []
        FakeWorker.responder = staticmethod(lambda text, w: (seen.append(text), "ok")[1])
        a = [{"role": "system", "content": "A"}, {"role": "user", "content": "a1"}]
        b = [{"role": "system", "content": "B"}, {"role": "user", "content": "b1"}]
        ra = self.post({"messages": a})
        self.post({"messages": b})          # 唯一のウインドウを B が取る
        a2 = a + [ra["choices"][0]["message"], {"role": "user", "content": "a2"}]
        self.post({"messages": a2})         # A は会話を作り直して全履歴を送る
        self.assertIn("優先です。\nA\n", seen[2])
        self.assertIn("a1", seen[2])
        self.assertIn("<assistant>", seen[2])
        self.assertIn("a2", seen[2])


if __name__ == "__main__":
    unittest.main()
