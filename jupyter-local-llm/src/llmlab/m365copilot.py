"""M365 Copilot（リサーチエージェント）への接続アダプタ。

Microsoft 365 Copilot の「Researcher（リサーチ）」エージェントに、章ごとの調査プロンプトを
投げて結果を受け取るための **差し替え可能なコネクタ** 群。上位の擬似GEPAリサーチ
（``copilotresearch.py``）はこのインタフェースだけに依存する。

コネクタの種類（``kind``）:

- ``demo``   : LLM も M365 も不要のモック。プロンプト（＝進化した指示）が「出典」「数値」
               「比較」「最新」等を含むほど濃い回答を返すので、擬似GEPAの改善曲線を体験できる。
- ``bridge`` : 人手ブリッジ。各章の完成プロンプトを UI に表示 → 人が M365 Copilot に貼り付け、
               返ってきた回答を貼り戻す。自動化/API が塞がれた社内環境でも確実に動く既定手段。
- ``selenium`` : Chrome/Chromium を Selenium WebDriver で駆動し、M365 Copilot の Web UI
               （Researcher）へ実際にプロンプトを投入して回答を読む。初回に SSO ログインが要る
               （永続プロファイル）。chromedriver の場所は ``driver_path`` で明示指定できる。
- ``graph``  : 任意の HTTP エンドポイント（Microsoft Graph / 社内 Copilot プロキシ等）へ
               Bearer トークンで POST する汎用コネクタ。要求/応答の JSON パスは設定可能。

すべての ``research()`` は :class:`ChapterResult` を返す（失敗しても例外にせず ok=False で返す）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

# 章プロンプト → 回答。emit で進捗、ask_bridge で人手ブリッジ（bridge コネクタのみ使用）。
EmitFn = Callable[[dict], None]
AskBridgeFn = Callable[[str, dict], dict]


@dataclass
class ChapterResult:
    """1 章分のリサーチ結果。"""

    text: str = ""
    citations: list[str] = field(default_factory=list)
    connector: str = ""
    latency_sec: float = 0.0
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "citations": self.citations,
            "connector": self.connector,
            "latency_sec": round(self.latency_sec, 2),
            "ok": self.ok,
            "error": self.error,
        }


_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+")


def extract_citations(text: str) -> list[str]:
    """回答テキストから出典（URL・「出典:」行）を素朴に拾う。重複は除く。"""
    if not text:
        return []
    found: list[str] = []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;)]")
        if u not in found:
            found.append(u)
    # 「出典:」「参考:」「Sources:」以降の箇条書きも拾う（URL が無い社内資料参照など）。
    # URL は上で拾い済みなので取り除いてから区切る（/ で URL を割らないため区切りに / は含めない）。
    for m in re.finditer(r"(?:出典|参考(?:文献)?|参照|Sources?|References?)\s*[:：]\s*(.+)",
                         text, re.IGNORECASE):
        tail = _URL_RE.sub("", m.group(1))
        for line in re.split(r"[\n・;、,]|\s{2,}", tail):
            line = re.sub(r"^\[\d+\]\s*", "", line.strip(" -*　[]"))
            if line and "://" not in line and line not in found and 1 < len(line) < 200:
                found.append(line)
    return found[:20]


# ---------------------------------------------------------------------------
# コネクタ基底
# ---------------------------------------------------------------------------

class BaseConnector:
    kind = "base"
    label = "base"

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    def research(self, prompt: str, *, meta: dict | None = None,
                 emit: EmitFn | None = None, ask_bridge: AskBridgeFn | None = None) -> ChapterResult:
        raise NotImplementedError

    def test(self) -> tuple[bool, str]:
        """接続確認。(ok, メッセージ) を返す。"""
        return True, f"{self.label}: 準備OK"

    @staticmethod
    def _emit(emit: EmitFn | None, **evt) -> None:
        if emit:
            emit(evt)


# ---------------------------------------------------------------------------
# demo — モック（LLM も M365 も不要）
# ---------------------------------------------------------------------------

class DemoConnector(BaseConnector):
    """指示（プロンプト）の充実度に応じて濃さが変わるモック回答を返す。

    擬似GEPA が指示へ「出典を付ける」「数値で比較する」「最新動向を含める」等を
    追記していくほど、回答が具体的・出典つきになる → 採点が上がる、という改善を再現する。
    """

    kind = "demo"
    label = "デモ（モック）"

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None) -> ChapterResult:
        meta = meta or {}
        time.sleep(float(self.options.get("delay", 0.35)))
        chapter = str(meta.get("chapter", "この章"))
        topic = str(meta.get("topic", "対象テーマ"))
        low = prompt.lower()

        wants_cite = any(k in prompt for k in ("出典", "根拠", "引用")) or "source" in low
        wants_num = any(k in prompt for k in ("数値", "定量", "データ", "統計")) or "quantif" in low
        wants_cmp = any(k in prompt for k in ("比較", "対比", "メリット", "デメリット", "trade"))
        wants_recent = any(k in prompt for k in ("最新", "近年", "トレンド", "2024", "2025", "2026"))

        parts = [f"【{chapter}】{topic} に関する調査結果（デモ）。",
                 f"{chapter} の基本的な論点と全体像を整理した。"]
        cites: list[str] = []
        if wants_num:
            parts.append("主要指標: 市場規模は前年比 +12.4%、導入企業は 3 年で約 2.1 倍に拡大（推計）。")
        if wants_cmp:
            parts.append("比較: 方式A（低コスト・拡張性中）と方式B（高精度・運用負荷高）の"
                         "トレードオフを 4 観点で対照。用途別には A→小規模、B→基幹が優位。")
        if wants_recent:
            parts.append("最新動向: 直近では規制対応と社内データ連携（RAG）の実装が主戦場になりつつある。")
        if wants_cite:
            parts.append("結論は下記の出典に基づく。")
            cites = [f"https://example.com/{meta.get('idx', 0)}/whitepaper",
                     f"社内資料: {topic} 調査メモ 2026 第{int(meta.get('idx', 0)) + 1}章"]
            parts.append("出典:\n[1] " + cites[0] + "\n[2] " + cites[1])
        else:
            parts.append("（注: 指示に出典要求が無いため、根拠URLは付していない）")
        text = "\n".join(parts)
        return ChapterResult(text=text, citations=cites, connector=self.kind, latency_sec=0.35)


# ---------------------------------------------------------------------------
# bridge — 人手ブリッジ（各章プロンプトを人が Copilot に貼り、回答を貼り戻す）
# ---------------------------------------------------------------------------

class BridgeConnector(BaseConnector):
    """人手ブリッジ。``ask_bridge(prompt, meta)`` で UI に問い合わせ、貼り戻しを待つ。

    上位（サーバ）が ask_bridge に SSE + 応答待ちの仕組みを渡す。自動化不可の環境で確実。
    """

    kind = "bridge"
    label = "人手ブリッジ（貼り付け）"

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None) -> ChapterResult:
        if ask_bridge is None:
            return ChapterResult(ok=False, connector=self.kind,
                                 error="bridge コネクタは対話サーバ経由でのみ利用できます")
        t0 = time.time()
        resp = ask_bridge(prompt, meta or {})
        if resp.get("decision") == "skip":
            return ChapterResult(ok=False, connector=self.kind, error="ユーザーがこの章をスキップ",
                                 latency_sec=time.time() - t0)
        text = str(resp.get("text", "")).strip()
        if not text:
            return ChapterResult(ok=False, connector=self.kind, error="貼り戻しが空でした",
                                 latency_sec=time.time() - t0)
        return ChapterResult(text=text, citations=extract_citations(text),
                             connector=self.kind, latency_sec=time.time() - t0)


# ---------------------------------------------------------------------------
# selenium — M365 Copilot Web UI を実ブラウザで駆動
# ---------------------------------------------------------------------------

class SeleniumConnector(BaseConnector):
    """Selenium（Edge / Chrome WebDriver）で M365 Copilot の Web UI を駆動する。

    既定は **Edge**（M365 Copilot は Edge + 業務アカウントの SSO で使うことが多いため）。
    ``browser="chrome"`` で Chrome/Chromium にも切替可。初回は SSO ログインが必要で、永続
    プロファイル（user_data_dir）にログイン状態を保存し 2 回目以降は無人で回せる。UI の
    セレクタは頻繁に変わるため options で上書きできる。

    options:
      browser         : "edge"（既定）/ "chrome"
      url             : Copilot のURL（既定 https://m365.cloud.microsoft/chat）
      driver_path     : WebDriver の場所（msedgedriver / chromedriver。**明示指定可**。
                        空なら Selenium Manager が自動解決）
      browser_binary  : ブラウザ実行ファイルの場所（空なら既定 / 環境変数を使用）
      user_data_dir   : ログイン状態を保存するフォルダ（既定 ~/.llmlab/copilot/selenium-profile）
      profile_directory: プロファイル名（既定 "Default"）
      headless        : True で無人（初回ログインは False 推奨）
      input_selector  : プロンプト入力欄のCSSセレクタ
      send_selector   : 送信ボタンのCSSセレクタ（省略時は Enter 送信）
      answer_selector : 回答要素のCSSセレクタ（最後の要素を回答とみなす）
      agent_selector  : Researcher 等のエージェント選択チップのCSSセレクタ（指定時は入力前にクリック）
      agent_hint      : 使用するエージェント名（既定 "Researcher"。agent_selector 未指定時は通知のみ）
      --- 待機（ウェイト）まわり -----------------------------------------------
      ready_timeout_ms: 入力欄/エージェント選択が現れるまでの待ち上限（既定 30000）
      initial_wait_ms : 送信直後に置く初期待機（既定 1500）
      poll_ms         : 回答ポーリング間隔（既定 800）
      busy_selector   : 「生成中」インジケータのCSS（例: 停止ボタン/スピナー）。**表示中は待機を続ける**
                        ＝伸びが一瞬止まっても生成継続中なら誤って打ち切らない。Researcher 向けに有効
      settle_ms       : 生成中でなく回答が伸びなくなってからの静止確定時間（既定 3000）
      timeout_ms      : 1 章あたりの回答待ち上限（既定 300000＝5分。Researcher は長いので必要なら増やす）

    driver_path / browser_binary は環境変数でも指定できる（browser により参照名が変わる）:
      edge  : EDGEDRIVER_PATH / MSEDGEDRIVER_PATH → driver_path、EDGE_BINARY / EDGE_BIN → browser_binary
      chrome: CHROMEDRIVER_PATH / CHROMEDRIVER    → driver_path、CHROME_BINARY / CHROME_BIN → browser_binary

    注意（並行実行）: ブラウザは 1 つの user_data_dir を同時に開けない。selenium コネクタで
    複数のリサーチを **同時に** 走らせる場合は、run ごとに別の user_data_dir を指定すること
    （同一プロファイルの同時起動は 2 本目が失敗する）。demo / bridge / graph は同時実行可。
    """

    kind = "selenium"
    label = "Selenium（Edge/Chrome）"

    DEFAULTS = {
        "browser": "edge",
        "url": "https://m365.cloud.microsoft/chat",
        "driver_path": "",
        "browser_binary": "",
        "profile_directory": "Default",
        "headless": False,
        "input_selector": "div[contenteditable='true'], textarea",
        "send_selector": "",
        "answer_selector": "[data-content='ai-message'], .ai-message, [class*='botMessage']",
        "agent_selector": "",
        "busy_selector": "",
        "ready_timeout_ms": 30000,
        "initial_wait_ms": 1500,
        "poll_ms": 800,
        "settle_ms": 3000,
        "timeout_ms": 300000,
        "agent_hint": "Researcher",
    }

    def _opt(self, key):
        return self.options.get(key, self.DEFAULTS.get(key))

    def _browser(self) -> str:
        return str(self._opt("browser") or "edge").lower()

    def _profile_dir(self) -> str:
        from .workspace import LLMLAB_DIR

        return self.options.get("user_data_dir") or str(LLMLAB_DIR / "copilot" / "selenium-profile")

    def _env_first(self, names: tuple[str, ...]) -> str:
        import os

        for n in names:
            v = os.environ.get(n)
            if v:
                return v.strip()
        return ""

    def _driver_path(self) -> str:
        v = self._opt("driver_path")
        if v:
            return str(v).strip()
        return self._env_first(("EDGEDRIVER_PATH", "MSEDGEDRIVER_PATH") if self._browser() == "edge"
                               else ("CHROMEDRIVER_PATH", "CHROMEDRIVER"))

    def _browser_binary(self) -> str:
        v = self._opt("browser_binary")
        if v:
            return str(v).strip()
        return self._env_first(("EDGE_BINARY", "EDGE_BIN") if self._browser() == "edge"
                               else ("CHROME_BINARY", "CHROME_BIN"))

    def test(self) -> tuple[bool, str]:
        try:
            import selenium  # noqa: F401
        except ImportError:
            return False, "selenium が未インストールです（`pip install selenium`）"
        import os

        dp = self._driver_path()
        if dp and not os.path.exists(dp):
            return False, f"driver_path が見つかりません: {dp}"
        bb = self._browser_binary()
        if bb and not os.path.exists(bb):
            return False, f"browser_binary が見つかりません: {bb}"
        drv = dp or "Selenium Manager で自動解決"
        return True, (f"selenium 利用可（browser={self._browser()} / driver={drv}）。"
                      f"初回は headless=False で SSO ログイン。profile: {self._profile_dir()}")

    def _make_driver(self):
        """設定に応じた Edge / Chrome WebDriver を作る（browser / driver_path / browser_binary を尊重）。"""
        from selenium import webdriver

        if self._browser() == "chrome":
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            make = webdriver.Chrome
        else:  # 既定: Edge
            from selenium.webdriver.edge.options import Options
            from selenium.webdriver.edge.service import Service
            make = webdriver.Edge

        opts = Options()
        if bool(self._opt("headless")):
            opts.add_argument("--headless=new")
        opts.add_argument(f"--user-data-dir={self._profile_dir()}")
        if self._opt("profile_directory"):
            opts.add_argument(f"--profile-directory={self._opt('profile_directory')}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        bb = self._browser_binary()
        if bb:
            opts.binary_location = bb
        dp = self._driver_path()
        # driver_path 明示時はそれを、未指定なら Selenium Manager（selenium 4.6+）に任せる
        service = Service(executable_path=dp) if dp else Service()
        return make(options=opts, service=service)

    @staticmethod
    def _type_prompt(driver, box, text: str) -> None:
        """複数行プロンプトを、途中の改行で送信されないように入力する（改行は Shift+Enter）。"""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        box.click()
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i:
                ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER)\
                    .key_up(Keys.SHIFT).perform()
            if line:
                box.send_keys(line)

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None) -> ChapterResult:
        ok, msg = self.test()
        if not ok:
            return ChapterResult(ok=False, connector=self.kind, error=msg)
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        t0 = time.time()
        driver = None
        ready = int(self._opt("ready_timeout_ms")) / 1000
        try:
            self._emit(emit, type="progress", stage="se_launch",
                       text=f"{self._browser()} 起動")
            driver = self._make_driver()
            driver.get(self._opt("url"))
            # エージェント選択（Researcher 等）。セレクタ未指定なら通知のみ。
            agent_sel = self._opt("agent_selector")
            if agent_sel:
                try:
                    WebDriverWait(driver, ready).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, agent_sel))).click()
                    self._emit(emit, type="progress", stage="se_agent",
                               text=f"エージェント選択: {self._opt('agent_hint')}")
                except Exception:  # noqa: BLE001  選択に失敗しても続行
                    self._emit(emit, type="progress", stage="se_agent",
                               text=f"エージェント選択に失敗（{self._opt('agent_hint')} を手動選択してください）")
            else:
                self._emit(emit, type="progress", stage="se_page",
                           text=f"Copilot を開いた（エージェント {self._opt('agent_hint')} を選択してください）")
            box = WebDriverWait(driver, ready).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self._opt("input_selector"))))
            self._type_prompt(driver, box, prompt)
            send = self._opt("send_selector")
            if send:
                driver.find_element(By.CSS_SELECTOR, send).click()
            else:
                box.send_keys(Keys.ENTER)
            self._emit(emit, type="progress", stage="se_wait", text="回答生成を待機")
            text = self._await_answer(driver, emit)
            return ChapterResult(text=text, citations=extract_citations(text),
                                 connector=self.kind, latency_sec=time.time() - t0,
                                 ok=bool(text), error="" if text else "回答が取得できませんでした（timeout_ms を増やすか busy_selector を設定）")
        except Exception as e:  # noqa: BLE001  ブラウザ失敗はUIへ返す
            return ChapterResult(ok=False, connector=self.kind, latency_sec=time.time() - t0,
                                 error=f"{type(e).__name__}: {e}")
        finally:
            try:
                if driver:
                    driver.quit()
            except Exception:  # noqa: BLE001
                pass

    def _await_answer(self, driver, emit: EmitFn | None = None) -> str:
        """回答が「生成完了」するまで待ってテキストを返す。

        - busy_selector が指定されていれば、その要素が **表示されている間は生成中とみなして待機**
          （回答の伸びが一瞬止まっても打ち切らない）。Researcher のような長時間ジョブに有効。
        - busy_selector が消え、かつ回答テキストが settle_ms の間伸びなければ確定。
        - initial_wait_ms（送信直後の初期待機）と poll_ms（ポーリング間隔）で無駄な空読みを避ける。
        - timeout_ms を上限に、~10 秒ごとに進捗（経過秒）を emit する。
        """
        from selenium.webdriver.common.by import By

        sel = self._opt("answer_selector")
        busy_sel = self._opt("busy_selector")
        start = time.time()
        deadline = start + int(self._opt("timeout_ms")) / 1000
        settle = int(self._opt("settle_ms")) / 1000
        poll = max(0.1, int(self._opt("poll_ms")) / 1000)

        time.sleep(int(self._opt("initial_wait_ms")) / 1000)  # 送信直後の初期待機
        last_text, stable_since, last_beat = "", None, 0.0
        while time.time() < deadline:
            busy = False
            if busy_sel:
                try:
                    busy = any(e.is_displayed()
                               for e in driver.find_elements(By.CSS_SELECTOR, busy_sel))
                except Exception:  # noqa: BLE001
                    busy = False
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                cur = els[-1].text if els else ""
            except Exception:  # noqa: BLE001
                cur = last_text
            if not busy and cur and cur == last_text:
                if stable_since and (time.time() - stable_since) >= settle:
                    return cur.strip()
                stable_since = stable_since or time.time()
            else:  # 生成中、または回答がまだ伸びている
                last_text = cur or last_text
                stable_since = None
            now = time.time()
            if emit and now - last_beat >= 10:
                self._emit(emit, type="progress", stage="se_wait",
                           text=f"回答生成待機中… {int(now - start)}s"
                                + ("（生成中）" if busy else ""))
                last_beat = now
            time.sleep(poll)
        return last_text.strip()


# ---------------------------------------------------------------------------
# graph — 汎用 HTTP（Microsoft Graph / 社内 Copilot プロキシ）
# ---------------------------------------------------------------------------

class GraphConnector(BaseConnector):
    """任意の HTTP エンドポイントへ Bearer で POST する汎用コネクタ。

    Microsoft Graph の Copilot 系 API や、社内に立てた Copilot プロキシを想定。要求/応答の
    JSON 形状はデプロイ先で異なるため設定で吸収する。

    options:
      endpoint       : POST 先 URL（必須）
      token          : Bearer トークン（または token_env で環境変数名を指定）
      token_env      : トークンを読む環境変数名（既定 M365_COPILOT_TOKEN）
      prompt_field   : リクエスト JSON 内でプロンプトを入れるキー（既定 "message"。ドット区切り可）
      extra_body     : 追加で混ぜるリクエスト JSON（dict）
      answer_path    : 応答 JSON から回答文字列を取り出すパス（ドット区切り、[i] 添字可）
      timeout_sec    : タイムアウト秒（既定 180）
    """

    kind = "graph"
    label = "HTTP/Graph API"

    def _token(self) -> str:
        import os

        return str(self.options.get("token")
                   or os.environ.get(self.options.get("token_env", "M365_COPILOT_TOKEN"), "")).strip()

    def test(self) -> tuple[bool, str]:
        if not self.options.get("endpoint"):
            return False, "endpoint（POST先URL）が未設定です"
        if not self._token():
            return False, "トークンが未設定です（options.token か環境変数 M365_COPILOT_TOKEN）"
        return True, f"endpoint={self.options['endpoint']}"

    @staticmethod
    def _set_path(obj: dict, path: str, value) -> None:
        keys = path.split(".")
        for k in keys[:-1]:
            obj = obj.setdefault(k, {})
        obj[keys[-1]] = value

    @staticmethod
    def _get_path(obj, path: str):
        for part in re.findall(r"[^.\[\]]+", path):
            if isinstance(obj, list):
                obj = obj[int(part)]
            elif isinstance(obj, dict):
                obj = obj.get(part)
            else:
                return None
        return obj

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None) -> ChapterResult:
        ok, msg = self.test()
        if not ok:
            return ChapterResult(ok=False, connector=self.kind, error=msg)
        import httpx

        body: dict = dict(self.options.get("extra_body") or {})
        self._set_path(body, self.options.get("prompt_field", "message"), prompt)
        headers = {"Authorization": f"Bearer {self._token()}",
                   "Content-Type": "application/json"}
        t0 = time.time()
        try:
            self._emit(emit, type="progress", stage="http", text="HTTP POST 送信")
            r = httpx.post(self.options["endpoint"], json=body, headers=headers,
                           timeout=float(self.options.get("timeout_sec", 180)))
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            return ChapterResult(ok=False, connector=self.kind, latency_sec=time.time() - t0,
                                 error=f"{type(e).__name__}: {e}")
        answer_path = self.options.get("answer_path")
        text = self._get_path(data, answer_path) if answer_path else data
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        return ChapterResult(text=text.strip(), citations=extract_citations(text),
                             connector=self.kind, latency_sec=time.time() - t0, ok=bool(text.strip()))


# ---------------------------------------------------------------------------
# ファクトリ
# ---------------------------------------------------------------------------

_CONNECTORS = {
    "demo": DemoConnector,
    "bridge": BridgeConnector,
    "selenium": SeleniumConnector,
    "graph": GraphConnector,
}


def make_connector(kind: str, options: dict | None = None) -> BaseConnector:
    """コネクタを生成する。未知の種類は demo にフォールバック。"""
    cls = _CONNECTORS.get((kind or "demo").lower(), DemoConnector)
    return cls(options or {})


def connector_kinds() -> list[dict]:
    """UI 用: 利用可能なコネクタの一覧（種類とラベル）。"""
    return [{"kind": k, "label": cls.label} for k, cls in _CONNECTORS.items()]
