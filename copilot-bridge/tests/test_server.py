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


class WarmupTests(ServerTestCase):
    """新しい会話で、決まりだけを先に送ってから会話を送る（暖機）。"""
    windows = 1

    def setUp(self):
        super().setUp()
        self.cfg.warmup = "client"
        self.seen = []

    def ask(self, msgs, key, *, warm=True, tools=None, model=None):
        headers = {"Content-Type": "application/json", "X-Conversation-Id": key}
        if warm:
            headers["X-Bridge-Warmup"] = "1"
        body = {"messages": msgs, **({"tools": tools} if tools else {}), **({"model": model} if model else {})}
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def record(self, *answers):
        answers = list(answers)

        def responder(text, worker):
            self.seen.append(text)
            return answers.pop(0) if answers else "【最終回答】ok"   # 印の無い返事は言い直しになる
        FakeWorker.responder = staticmethod(responder)

    def test_rules_first_then_the_conversation(self):
        from bridge.protocol import WARMUP_HINT
        self.record("了解", "【最終回答】一覧です")
        msgs = [{"role": "system", "content": "方針 P"}, {"role": "user", "content": "w1 を見て"}]
        chats = self.get("/status")["windows"][0]["chats"]
        r1 = self.ask(msgs, "warm/1", tools=TOOLS)
        self.assertEqual(len(self.seen), 2)
        self.assertIn("【書式】", self.seen[0])
        self.assertIn("方針 P", self.seen[0])
        self.assertIn("read_file", self.seen[0])
        self.assertTrue(self.seen[0].endswith(WARMUP_HINT))
        self.assertNotIn("w1 を見て", self.seen[0])                 # 1 通目に指示は入れない
        self.assertTrue(self.seen[1].startswith("【会話】"))
        self.assertIn("w1 を見て", self.seen[1])
        self.assertNotIn("【書式】", self.seen[1])
        self.assertEqual(r1["choices"][0]["message"]["content"], "一覧です")   # 返すのは 2 通目への返事
        self.assertEqual(self.get("/status")["windows"][0]["chats"], chats + 1)  # 会話は 1 つ
        # 続きは差分だけ。暖機はしない
        self.ask(msgs + [r1["choices"][0]["message"], {"role": "user", "content": "w2"}], "warm/1", tools=TOOLS)
        self.assertEqual(len(self.seen), 3)
        self.assertNotIn("【書式】", self.seen[2])
        # 履歴が書き換わって会話を作り直すときは、もう一度暖機する
        self.ask([{"role": "system", "content": "方針 P"}, {"role": "user", "content": "書き換え後"}], "warm/1", tools=TOOLS)
        self.assertEqual(len(self.seen), 5)
        self.assertTrue(self.seen[3].endswith(WARMUP_HINT))
        self.assertIn("書き換え後", self.seen[4])

    def test_the_answer_to_the_rules_is_thrown_away(self):
        # 決まりへの返事が操作の形でも、それは読まない（実行させない・呼び出し元に返さない）
        self.record('<tool_call>{"name": "read_file", "arguments": {"path": "x"}}</tool_call>', "【最終回答】終わり")
        r = self.ask([{"role": "user", "content": "w3"}], "warm/2", tools=TOOLS)
        self.assertEqual(r["choices"][0]["message"]["content"], "終わり")
        self.assertNotIn("tool_calls", r["choices"][0]["message"])
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")

    def test_only_when_asked_or_set_and_only_with_rules(self):
        msgs = [{"role": "system", "content": "方針"}, {"role": "user", "content": "w4"}]
        cases = (("client", False, msgs, 1), ("client", True, msgs, 2), ("always", False, msgs, 2),
                 ("off", True, msgs, 1), ("OFF", True, msgs, 1), ("", True, msgs, 2),
                 ("always", True, msgs[1:], 1))                     # 決まりが無ければ（操作も指示も無い）しない
        for index, (mode, asked, sent, count) in enumerate(cases):
            self.cfg.warmup = mode
            self.seen.clear()
            self.record()
            self.ask(sent, f"warm/3-{index}", warm=asked)
            self.assertEqual(len(self.seen), count, (mode, asked))
            if count == 1:
                self.assertIn("【会話】", self.seen[0])               # まとめて 1 通（これまでどおり）
        # 指示だけ（操作一覧なし）でも、頼まれればする: 呼び出し元が自分の書式を指示に書いている場合
        self.cfg.warmup = "client"
        self.seen.clear()
        self.record()
        self.ask(msgs, "warm/3-own", warm=True)
        self.assertEqual(len(self.seen), 2)
        self.assertIn("方針", self.seen[0])
        self.assertNotIn("w4", self.seen[0])

    def test_restart_during_warmup_starts_the_chat_again_once(self):
        from bridge.pool import WorkerRestarted
        state = {"raised": 0}

        def responder(text, worker):
            self.seen.append(text)
            if state["raised"] < 1:
                state["raised"] += 1
                raise WorkerRestarted("w0: ブラウザを再起動しました")
            return "【最終回答】復帰"
        FakeWorker.responder = staticmethod(responder)
        chats = self.get("/status")["windows"][0]["chats"]
        r = self.ask([{"role": "system", "content": "方針"}, {"role": "user", "content": "w5"}], "warm/4")
        self.assertEqual(r["choices"][0]["message"]["content"], "復帰")
        # 決まり（再起動で失敗）→ 会話を作り直して 決まり → 会話
        self.assertEqual(["【会話】" in text for text in self.seen], [False, False, True])
        self.assertEqual(self.get("/status")["windows"][0]["chats"], chats + 2)

    def test_second_restart_is_an_error(self):
        from bridge.pool import WorkerRestarted

        def responder(text, worker):
            self.seen.append(text)
            raise WorkerRestarted("w0: ブラウザを再起動しました")
        FakeWorker.responder = staticmethod(responder)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.ask([{"role": "system", "content": "方針"}, {"role": "user", "content": "w6"}], "warm/5")
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(len(self.seen), 2)                         # やり直しは 1 回だけ


class WarmupFallbackTests(ServerTestCase):
    windows = 1

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cfg.models = {"gpt": "GPT-5", "opus": "Claude Opus"}
        cls.cfg.model_fallbacks = ["opus", "gpt"]
        cls.cfg.warmup = "always"

    def test_model_stalled_on_the_rules_falls_back_and_warms_the_next_model(self):
        from bridge.pool import ModelStalled
        seen = []

        def responder(text, worker):
            seen.append((worker.models_selected[-1], "【会話】" in text))
            if worker.models_selected[-1] == "Claude Opus":
                raise ModelStalled("w0: 90 秒たっても生成が始まりません")
            return "【最終回答】gpt で回答"
        FakeWorker.responder = staticmethod(responder)
        r = self.post({"model": "opus", "messages": [{"role": "system", "content": "方針"}, {"role": "user", "content": "wf1"}]})
        self.assertEqual(r["choices"][0]["message"]["content"], "gpt で回答")
        # opus は決まりの時点で詰まる → gpt で会話を作り直し、決まり → 会話
        self.assertEqual(seen, [("Claude Opus", False), ("GPT-5", False), ("GPT-5", True)])


class OwnFormatTests(ServerTestCase):
    """呼び出し元が自分の書式を持つとき、この中継の規則・書式・念押し・言い直しを付けない。"""
    windows = 1
    SYSTEM = {"role": "system", "content": "呼び出し元の決まり J: JSON を 1 つだけ返す"}

    def setUp(self):
        super().setUp()
        self.cfg.own_format, self.cfg.warmup = "client", "client"
        self.seen = []

    def ask(self, msgs, key, *, own=True, warm=False, tools=None):
        headers = {"Content-Type": "application/json", "X-Conversation-Id": key}
        if own:
            headers["X-Bridge-Format"] = "own"
        if warm:
            headers["X-Bridge-Warmup"] = "1"
        body = {"messages": msgs, **({"tools": tools} if tools else {})}
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def record(self, *answers):
        answers = list(answers)

        def responder(text, worker):
            self.seen.append(text)
            return answers.pop(0) if answers else "【最終回答】ok"
        FakeWorker.responder = staticmethod(responder)

    def test_only_the_callers_instructions_are_sent_and_the_answer_comes_back_as_written(self):
        answers = ('{"awb_protocol":"copilot-json-v1","kind":"final","text":"done"}',
                   '<tool_call>\n{"name": "read_file", "arguments": {"path": "x"}}\n</tool_call>',
                   '【最終回答】{"name": "read_file", "arguments": {"path": "x"}}',
                   "この環境ではツールは利用できません。ファイルは存在しません。")
        for index, answer in enumerate(answers):
            self.seen.clear()
            self.record(answer)
            r = self.ask([self.SYSTEM, {"role": "user", "content": f"o{index}"}], f"own/1-{index}")
            # 送るのは呼び出し元の指示と会話だけ
            self.assertEqual(self.seen, [self.SYSTEM["content"] + f"\n\no{index}"])
            # 回答はそのまま: 操作として読まない・印を取らない・言い直させない
            message = r["choices"][0]["message"]
            self.assertEqual(message["content"], answer)
            self.assertNotIn("tool_calls", message)
            self.assertEqual(r["choices"][0]["finish_reason"], "stop")

    def test_the_callers_own_record_of_the_answer_continues_the_same_chat(self):
        self.record("回答 1（Copilot が書いたまま）", "回答 2")
        msgs = [self.SYSTEM, {"role": "user", "content": "o-first"}]
        chats = self.get("/status")["windows"][0]["chats"]
        self.ask(msgs, "own/2")
        # 呼び出し元は、回答を自分の記録（呼び出し ID つき）に直して送り返す
        record = {"role": "assistant", "content": '{"history":"assistant","requests":[{"call_id":"c1","tool":"read_file"}]}'}
        result = {"role": "user", "content": '{"history":"tool","call_id":"c1","result_text":"中身"}'}
        r2 = self.ask(msgs + [record, result], "own/2")
        self.assertEqual(r2["choices"][0]["message"]["content"], "回答 2")
        self.assertEqual(self.get("/status")["windows"][0]["chats"], chats + 1)      # 同じ会話の続き
        self.assertEqual(self.seen[1], "<assistant>\n" + record["content"] + "\n</assistant>\n\n" + result["content"])
        # この中継の書式のままなら、回答の原文と一致しないので作り直す（これまでどおり）
        self.seen.clear()
        self.record()
        self.ask(msgs, "own/3", own=False)
        self.ask(msgs + [record, result], "own/3", own=False)
        self.assertIn("【書式】", self.seen[1])

    def test_with_warmup_the_instructions_go_first(self):
        from bridge.protocol import WARMUP_HINT
        self.record("了解", '{"kind":"final","text":"x"}')
        r = self.ask([self.SYSTEM, {"role": "user", "content": "o-warm"}], "own/4", warm=True)
        self.assertEqual(self.seen, [self.SYSTEM["content"] + "\n\n" + WARMUP_HINT, "o-warm"])
        self.assertEqual(r["choices"][0]["message"]["content"], '{"kind":"final","text":"x"}')

    def test_only_when_asked_without_tools_with_instructions_and_not_turned_off(self):
        user = {"role": "user", "content": "o-cases"}
        cases = (("client", True, [self.SYSTEM, user], None, True),
                 ("client", False, [self.SYSTEM, user], None, False),     # 頼まれていない
                 ("off", True, [self.SYSTEM, user], None, False),         # 設定で止めてある
                 ("client", True, [self.SYSTEM, user], TOOLS, False),     # tools がある: 読むのはこの中継
                 ("client", True, [user], None, False))                   # 指示が無い: 伝える書式が無い
        for index, (mode, asked, msgs, tools, own) in enumerate(cases):
            self.cfg.own_format = mode
            self.seen.clear()
            self.record()
            self.ask(msgs, f"own/5-{index}", own=asked, tools=tools)
            self.assertEqual("【書式】" not in self.seen[0], own, (mode, asked, bool(tools)))


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


class FallbackTests(ServerTestCase):
    windows = 1

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cfg.models = {"gpt": "GPT-5", "opus": "Claude Opus"}
        cls.cfg.model_fallbacks = ["opus", "gpt"]
        cls.cfg.model_cooldown_sec = 600

    def test_stalled_model_falls_back_and_cools_down(self):
        from bridge.pool import ModelStalled

        seen = []

        def responder(text, worker):
            seen.append(worker.models_selected[-1])
            if worker.models_selected[-1] == "Claude Opus":
                raise ModelStalled("w0: 90 秒たっても生成が始まりません")
            return "【最終回答】gpt で回答"

        FakeWorker.responder = staticmethod(responder)
        r = self.post({"model": "opus", "messages": [{"role": "user", "content": "fb1"}]})
        self.assertEqual(r["choices"][0]["message"]["content"], "gpt で回答")
        self.assertEqual(seen, ["Claude Opus", "GPT-5"])      # opus で詰まり → gpt で作り直し
        self.assertIn("Claude Opus", self.get("/status")["cooldown"])
        # 冷却中は最初から gpt へ
        seen.clear()
        self.post({"model": "opus", "messages": [{"role": "user", "content": "fb2"}]})
        self.assertEqual(seen, ["GPT-5"])

    def test_repeated_stall_switches_until_midnight(self):
        from bridge.pool import ModelStalled

        self.bridge._cooldown.clear(); self.bridge._stalls.clear()
        self.cfg.model_exhaust_after = 2

        def responder(text, worker):
            if worker.models_selected[-1] == "Claude Opus":
                raise ModelStalled("枠切れ")
            return "【最終回答】gpt"

        FakeWorker.responder = staticmethod(responder)
        self.post({"model": "opus", "messages": [{"role": "user", "content": "ex1"}]})
        self.assertLess(self.get("/status")["cooldown"]["Claude Opus"], 700)       # 1 回目は短い冷却
        self.bridge._cooldown["Claude Opus"] = 0                                   # 冷却明けを再現
        self.post({"model": "opus", "messages": [{"role": "user", "content": "ex2"}]})
        st = self.get("/status")
        self.assertGreater(st["cooldown"]["Claude Opus"], 700)                     # 2 回目で日付が変わるまで
        self.assertEqual(st["stalls"]["Claude Opus"], 2)
        self.bridge._cooldown.clear(); self.bridge._stalls.clear()

    def test_success_resets_stall_count(self):
        self.bridge._cooldown.clear(); self.bridge._stalls["GPT-5"] = 1
        FakeWorker.responder = staticmethod(lambda text, w: "【最終回答】ok")
        self.post({"model": "gpt", "messages": [{"role": "user", "content": "rs1"}]})
        self.assertNotIn("GPT-5", self.get("/status")["stalls"])

    def test_all_models_stalled_is_503(self):
        from bridge.pool import ModelStalled

        def responder(text, worker):
            raise ModelStalled("詰まり")

        FakeWorker.responder = staticmethod(responder)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post({"model": "gpt", "messages": [{"role": "user", "content": "fb3"}]})
        self.assertEqual(cm.exception.code, 503)
        self.bridge._cooldown.clear(); self.bridge._stalls.clear()


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
