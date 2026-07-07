"""M365 Copilot（リサーチエージェント）への接続アダプタ。

Microsoft 365 Copilot の「Researcher（リサーチ）」エージェントに、章ごとの調査プロンプトを
投げて結果を受け取るための **差し替え可能なコネクタ** 群。上位の擬似GEPAリサーチ
（``copilotresearch.py``）はこのインタフェースだけに依存する。

コネクタの種類（``kind``）:

- ``demo``   : LLM も M365 も不要のモック。プロンプト（＝進化した指示）が「出典」「数値」
               「比較」「最新」等を含むほど濃い回答を返すので、擬似GEPAの改善曲線を体験できる。
- ``bridge`` : 人手ブリッジ。各章の完成プロンプトを UI に表示 → 人が M365 Copilot に貼り付け、
               返ってきた回答を貼り戻す。自動化/API が塞がれた社内環境でも確実に動く既定手段。
- ``playwright`` : Chromium を Playwright で駆動し、M365 Copilot の Web UI（Researcher）へ
               実際にプロンプトを投入して回答を読む。初回に SSO ログインが要る（永続プロファイル）。
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
# playwright — M365 Copilot Web UI を実ブラウザで駆動
# ---------------------------------------------------------------------------

class PlaywrightConnector(BaseConnector):
    """Chromium（Playwright）で M365 Copilot の Web UI を駆動する。

    初回は SSO ログインが必要。永続プロファイル（user_data_dir）にログイン状態を保存し、
    2 回目以降は無人で回せる。UI のセレクタは頻繁に変わるため options で上書きできる。

    options:
      url            : Copilot のURL（既定 https://m365.cloud.microsoft/chat）
      user_data_dir  : ログイン状態を保存するフォルダ（既定 ~/.llmlab/copilot/pw-profile）
      headless       : True で無人（初回ログインは False 推奨）
      input_selector : プロンプト入力欄のセレクタ
      send_selector  : 送信ボタンのセレクタ（省略時は Enter 送信）
      answer_selector: 回答要素のセレクタ（最後の要素を回答とみなす）
      settle_ms      : 回答が伸びなくなったと判定するまでの静止時間（ms）
      timeout_ms     : 1 章あたりの最大待ち時間（ms）
      agent_hint     : 使用するエージェント名（既定 "Researcher"）。UI 選択は環境依存のため通知のみ。
    """

    kind = "playwright"
    label = "Playwright（実ブラウザ）"

    DEFAULTS = {
        "url": "https://m365.cloud.microsoft/chat",
        "headless": False,
        "input_selector": "div[contenteditable='true'], textarea",
        "send_selector": "",
        "answer_selector": "[data-content='ai-message'], .ai-message, [class*='botMessage']",
        "settle_ms": 2500,
        "timeout_ms": 180000,
        "agent_hint": "Researcher",
    }

    def _opt(self, key):
        return self.options.get(key, self.DEFAULTS.get(key))

    def _profile_dir(self) -> str:
        from .workspace import LLMLAB_DIR

        d = self.options.get("user_data_dir") or str(LLMLAB_DIR / "copilot" / "pw-profile")
        return d

    def test(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False, ("playwright が未インストールです。`pip install playwright` の後 "
                           "`playwright install chromium`（本環境では既定ブラウザを自動使用）")
        return True, ("playwright 利用可。初回は headless=False でログインしてください "
                      f"(profile: {self._profile_dir()})")

    def _launch(self):
        """永続プロファイルで Chromium を起動。環境の既定ブラウザがあれば流用。"""
        import os

        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        launch_kwargs = {"user_data_dir": self._profile_dir(),
                         "headless": bool(self._opt("headless"))}
        # 本実行環境は PLAYWRIGHT_BROWSERS_PATH に Chromium が同梱されている場合がある
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or self.options.get("executable_path")
        if exe:
            launch_kwargs["executable_path"] = exe
        ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
        return pw, ctx

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None) -> ChapterResult:
        ok, msg = self.test()
        if not ok:
            return ChapterResult(ok=False, connector=self.kind, error=msg)
        t0 = time.time()
        pw = ctx = None
        try:
            self._emit(emit, type="progress", stage="pw_launch", text="ブラウザ起動")
            pw, ctx = self._launch()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(self._opt("url"), wait_until="domcontentloaded",
                      timeout=int(self._opt("timeout_ms")))
            self._emit(emit, type="progress", stage="pw_page",
                       text=f"Copilot を開いた（エージェント: {self._opt('agent_hint')} を選択してください）")
            box = page.locator(self._opt("input_selector")).last
            box.wait_for(timeout=int(self._opt("timeout_ms")))
            box.click()
            box.fill(prompt) if page.locator("textarea").count() else box.type(prompt, delay=2)
            send = self._opt("send_selector")
            if send:
                page.locator(send).last.click()
            else:
                page.keyboard.press("Enter")
            self._emit(emit, type="progress", stage="pw_wait", text="回答生成を待機")
            text = self._await_answer(page)
            return ChapterResult(text=text, citations=extract_citations(text),
                                 connector=self.kind, latency_sec=time.time() - t0,
                                 ok=bool(text), error="" if text else "回答が取得できませんでした")
        except Exception as e:  # noqa: BLE001  ブラウザ失敗はUIへ返す
            return ChapterResult(ok=False, connector=self.kind, latency_sec=time.time() - t0,
                                 error=f"{type(e).__name__}: {e}")
        finally:
            try:
                if ctx:
                    ctx.close()
                if pw:
                    pw.stop()
            except Exception:  # noqa: BLE001
                pass

    def _await_answer(self, page) -> str:
        """回答要素が伸びなくなる（settle）まで待ってテキストを返す。"""
        sel = self._opt("answer_selector")
        deadline = time.time() + int(self._opt("timeout_ms")) / 1000
        settle = int(self._opt("settle_ms")) / 1000
        last_text, stable_since = "", None
        while time.time() < deadline:
            try:
                loc = page.locator(sel).last
                cur = loc.inner_text(timeout=2000) if loc.count() else ""
            except Exception:  # noqa: BLE001
                cur = last_text
            if cur and cur == last_text:
                if stable_since and (time.time() - stable_since) >= settle:
                    return cur.strip()
                stable_since = stable_since or time.time()
            else:
                last_text, stable_since = cur, None
            page.wait_for_timeout(600)
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
    "playwright": PlaywrightConnector,
    "graph": GraphConnector,
}


def make_connector(kind: str, options: dict | None = None) -> BaseConnector:
    """コネクタを生成する。未知の種類は demo にフォールバック。"""
    cls = _CONNECTORS.get((kind or "demo").lower(), DemoConnector)
    return cls(options or {})


def connector_kinds() -> list[dict]:
    """UI 用: 利用可能なコネクタの一覧（種類とラベル）。"""
    return [{"kind": k, "label": cls.label} for k, cls in _CONNECTORS.items()]
