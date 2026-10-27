"""edge.py の画面操作を、模擬ページ（copilot_mock.html）× 実ブラウザで通す（Copilot 不要）。

既定は Edge（Windows の想定。msedgedriver は config と同じ driver_path）。
環境変数で Chrome/Chromium に切り替えられる:

  COPILOT_BRIDGE_TEST_BROWSER=chrome
  COPILOT_BRIDGE_TEST_BINARY=/path/to/chrome      （任意）
  COPILOT_BRIDGE_TEST_DRIVER=/path/to/chromedriver（任意。無ければ Selenium Manager）
  COPILOT_BRIDGE_TEST_HEADLESS=1                   （画面なし）

selenium が無い、またはドライバが起動できなければ skip する。CI では実行しない。
"""

import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import Config  # noqa: E402
from bridge.edge import THROTTLE_FLAGS, EdgeWorker  # noqa: E402
from bridge.protocol import normalize_message, parse_reply, render_delta, render_opening  # noqa: E402

MOCK_URL = Path(__file__).with_name("copilot_mock.html").resolve().as_uri()
BROWSER = os.environ.get("COPILOT_BRIDGE_TEST_BROWSER", "edge").lower()
HEADLESS = os.environ.get("COPILOT_BRIDGE_TEST_HEADLESS", "") == "1"


class MockWorker(EdgeWorker):
    """起動だけ差し替える（Chrome 指定時）。操作は EdgeWorker のまま。"""

    def start(self):
        if BROWSER != "chrome":
            return super().start()
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        profile = self.cfg.resolved_profile_dir() / self.id
        profile.mkdir(parents=True, exist_ok=True)
        opts = Options()
        if os.environ.get("COPILOT_BRIDGE_TEST_BINARY"):
            opts.binary_location = os.environ["COPILOT_BRIDGE_TEST_BINARY"]
        opts.add_argument(f"--user-data-dir={profile}")
        if HEADLESS:
            opts.add_argument("--headless=new")
        for f in THROTTLE_FLAGS + ["--no-sandbox", "--no-proxy-server", "--disable-background-networking",
                                   "--disable-component-update", "--disable-dev-shm-usage"]:
            opts.add_argument(f)
        drv = os.environ.get("COPILOT_BRIDGE_TEST_DRIVER")
        service = Service(drv) if drv else None
        self.driver = webdriver.Chrome(service=service, options=opts)
        self.new_chat(timeout=self.cfg.login_timeout_sec)


def _cfg(tmp: Path) -> Config:
    return Config(backend="edge", chat_url=MOCK_URL, reply_timeout_sec=30, login_timeout_sec=30,
                  profile_dir=str(tmp / "profiles"),
                  driver_path=os.environ.get("COPILOT_BRIDGE_TEST_DRIVER") or Config().driver_path)


class EdgeMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import selenium  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("selenium が無い")
        import tempfile

        cls.tmp = Path(tempfile.mkdtemp(prefix="copilot-bridge-mock-"))
        cls.cfg = _cfg(cls.tmp)
        cls.workers = []
        try:
            cls.workers = [MockWorker(i, cls.cfg) for i in range(3)]
            for w in cls.workers:
                w.start()
        except Exception as e:  # noqa: BLE001
            for w in cls.workers:
                w.close()
            raise unittest.SkipTest(f"ブラウザを起動できない: {type(e).__name__}: {e}")

    @classmethod
    def tearDownClass(cls):
        for w in getattr(cls, "workers", []):
            w.close()

    def test_long_prompt_tool_call_and_continuation(self):
        w = self.workers[0]
        w.new_chat()   # 他のテストの会話をリセット
        msgs = [normalize_message({"role": "system", "content": "sys"}),
                normalize_message({"role": "user", "content": "A" * 20000 + "\n行2\n行3 END"})]
        tools = [{"type": "function", "function": {"name": "read_file", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}}}}]
        r1 = w.send(render_opening(msgs, tools))
        self.assertIn("head=あなたは", r1)   # 規則文の先頭
        self.assertIn("tail=戻されます。", r1)   # 2 万字を越えて末尾（念押し文）まで届いた
        _, calls = parse_reply(r1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        r2 = w.send(render_delta([normalize_message(
            {"role": "tool", "name": "read_file", "content": "hello"})]))
        self.assertTrue(r2.startswith("turn=2"))  # 同じ会話の続き
        self.assertIn("完了しました", r2)

    def test_delete_current_chat(self):
        from selenium.webdriver.common.by import By

        w = self.workers[0]
        w.send("削除試験")
        ok, why = w.delete_current_chat()
        self.assertTrue(ok, why)
        self.assertEqual(w.driver.find_element(By.ID, "deleted").text, "1")
        self.assertEqual(w.chat_turns, 0)

    def test_dom_read_without_known_selector(self):
        """selectors.response が合っていなくても、コピーボタンの祖先から回答 1 件分の本文を読める。"""
        import copy

        cfg = copy.deepcopy(self.cfg)
        cfg.read_mode = "dom"
        cfg.selectors = dict(cfg.selectors, response="#no-such-element")
        w = MockWorker(3, cfg)
        w.start()
        try:
            w.send("1 回目")
            r2 = w.send("2 回目 " + "x" * 20)
            self.assertTrue(r2.startswith("turn=2"), r2)
            self.assertNotIn("turn=1", r2)       # 前の回答を含まない
            self.assertNotIn("いいね", r2)        # ボタンの文字を含まない
            self.assertNotIn("[user", r2)        # 自分の発言を含まない
        finally:
            w.close()

    def test_select_model(self):
        from selenium.webdriver.common.by import By

        w = self.workers[0]
        w.new_chat(model="Claude Opus")
        self.assertEqual(w.driver.find_element(By.ID, "model").text, "Claude Opus")
        self.assertEqual(w.current_model, "Claude Opus")
        self.assertEqual(w.current_model_label(), "Claude Opus")   # aria-label から今のモデルを読める
        ok, _ = w.select_model("Sonnet5")                           # Sonnet5.5 ではなく完全一致の Sonnet5
        self.assertTrue(ok)
        self.assertEqual(w.driver.find_element(By.ID, "model").text, "Sonnet5")
        ok, _ = w.select_model("Sonnet5")                           # すでに選択済みなら何もしない
        self.assertTrue(ok)
        ok, why = w.select_model("No Such Model")
        self.assertFalse(ok)

    def test_parallel_windows_do_not_mix(self):
        ws = self.workers[1:]
        res = {}

        def run(w):
            res[w.id] = w.send(f"合言葉 {w.id} " + "x" * 50)

        ts = [threading.Thread(target=run, args=(w,)) for w in ws]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        for w in ws:
            self.assertIn(f"head=合言葉 {w.id}"[:11], res[w.id])


if __name__ == "__main__":
    unittest.main()
