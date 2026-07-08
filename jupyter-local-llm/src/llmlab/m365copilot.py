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


def _has_pyperclip() -> bool:
    try:
        import pyperclip  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


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

    def close(self) -> None:
        """セッションの後始末（ブラウザ終了など）。既定は何もしない。run 終了時に呼ばれる。"""

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
    """Selenium（Edge 既定 / Chrome）で M365 Copilot の **Researcher（リサーチ）** を実駆動する。

    実運用で動作実績のあるフローをそのまま実装している:

      1. office.com を開く（``login_url``）。必要なら SSO を **ブラウザで完了**（``login_timeout_ms`` 待つ）
      2. ヘッダのメニュー（``menu_button_xpath``）→ エージェント入口（``agent_entrance_xpath``）をクリックして
         Researcher へ移動。案内タブ（``close_tab_selector``）を閉じる
      3. 入力欄（``editor_id`` = 既定 ``m365-chat-editor-target-element``）にプロンプトを入れて送信
      4. レポートの長さ（``length_selector`` = 既定「長い, 5 ページ以上」）が出れば選択
      5. 続行の合図（``proceed_text`` = 既定 "go ahead" ＝おまかせ）を送る
      6. **生成停止ボタン（``stop_selector`` = 「生成を停止する」）が出現→消滅** したら生成完了とみなす
         （出ない/止まらないときは ``refresh_every_ms`` ごとにリロード、``timeout_ms`` で打ち切り）
      7. 「応答のコピー」（``copy_selector``）を押し、**クリップボードから本文を取得**（``pyperclip`` 推奨）

    セレクタは tenant / 言語 / UI 改定で変わるため **すべて options で上書き可能**。既定値は日本語UI向け。
    ドライバ（session）は run 内で **使い回す**（章ごとにログインし直さない）。2 章目以降は新規リサーチ
    （``new_chat_selector`` があればクリック、無ければ入口から開き直し）。run 終了時に ``close()`` される。

    主な options:
      browser          : "edge"（既定）/ "chrome"
      driver_path      : msedgedriver / chromedriver の場所（**明示指定可**。空なら自動解決 / 環境変数）
      browser_binary   : ブラウザ実行ファイルの場所（空なら既定 / 環境変数）
      user_data_dir    : プロファイル（空＝ブラウザ既定プロファイル＝業務SSOを流用。同時実行時のみ別途指定）
      profile_directory: プロファイル名（空なら付与しない）
      headless         : True で無人（初回 SSO は False 必須）
      login_url        : 既定 https://www.office.com/?auth=2
      login_timeout_ms : サインイン完了（メニュー出現）待ち上限（既定 300000＝5分）
      menu_button_xpath / agent_entrance_xpath / close_tab_selector : Researcher へ入る導線
      editor_id        : 入力欄の id（既定 m365-chat-editor-target-element）
      length_selector  : レポート長さの選択ボタン（既定「長い, 5 ページ以上」。空で無効）
      proceed_text     : 続行の合図（既定 "go ahead"）。send_proceed=False で無効
      stop_selector    : 生成中を示す停止ボタン（既定「生成を停止する」）
      copy_selector    : 応答コピーのボタン（既定「応答のコピー」）
      answer_selector  : クリップボードが取れない時の DOM フォールバック（任意）
      new_chat_selector: 2 章目以降の新規リサーチ開始ボタン（任意）
      ready_timeout_ms / poll_ms / refresh_every_ms / timeout_ms : 待機（ウェイト）調整
                         （timeout_ms 既定 1800000＝30分。Researcher は長い）

    driver_path / browser_binary は環境変数でも指定できる（browser で参照名が変わる）:
      edge  : EDGEDRIVER_PATH / MSEDGEDRIVER_PATH → driver_path、EDGE_BINARY / EDGE_BIN → browser_binary
      chrome: CHROMEDRIVER_PATH / CHROMEDRIVER    → driver_path、CHROME_BINARY / CHROME_BIN → browser_binary

    注意:
    - 応答本文の取得は「応答のコピー」→クリップボードが最も確実。``pip install pyperclip`` 推奨
      （無ければ JS/tkinter で読み、それも不可なら answer_selector の DOM 抽出を試みる）。
    - Researcher は 1 章あたり数分〜十数分かかる。擬似GEPA の予算/minibatch は小さめ推奨
      （budget 0〜1）。
    - 並行実行時のみ run ごとに別 user_data_dir を指定（同一プロファイルの同時起動は失敗する）。
    """

    kind = "selenium"
    label = "Selenium（Edge/Chrome・Researcher）"

    DEFAULTS = {
        "browser": "edge",
        "driver_path": "",
        "browser_binary": "",
        "user_data_dir": "",          # 空 = ブラウザ既定プロファイル（業務SSOを流用）
        "profile_directory": "",
        "headless": False,
        "login_url": "https://www.office.com/?auth=2",
        "login_timeout_ms": 300000,
        # Researcher への導線（動作実績のある既定。tenant/言語で変わるため上書き可）
        "menu_button_xpath":
            "/html/body/div[1]/div/div/main/div[1]/div/header/div/div[1]/button",
        "agent_entrance_xpath":
            "/html/body/div[1]/div/div/main/div[1]/div/div/div[2]/div/div[1]/div[2]"
            "/div[1]/div/button[1]/span[2]",
        "close_tab_selector": "button[data-testid='header-branding-container']",
        "post_open_wait_ms": 10000,
        # 入力・レポート長さ・続行
        "editor_id": "m365-chat-editor-target-element",
        "length_selector": "button[aria-label='長い, 5 ページ以上']",
        "length_timeout_ms": 50000,
        "send_proceed": True,
        "proceed_text": "go ahead",
        # 生成待ち（停止ボタンの出現→消滅で完了）
        "stop_selector": "button[aria-label='生成を停止する']",
        "poll_ms": 1000,
        "refresh_every_ms": 600000,
        "timeout_ms": 1800000,        # 30 分
        # 応答取得
        "copy_selector": "button[aria-label='応答のコピー']",
        "answer_selector": "",        # DOM フォールバック（任意）
        # 2 章目以降の新規リサーチ
        "new_chat_selector": "",
        "ready_timeout_ms": 60000,
        "agent_hint": "Researcher",
    }

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        self._driver = None
        self._research_count = 0

    def _opt(self, key):
        return self.options.get(key, self.DEFAULTS.get(key))

    def _browser(self) -> str:
        return str(self._opt("browser") or "edge").lower()

    def _profile_dir(self) -> str:
        # 空なら --user-data-dir を付けず、ブラウザ既定プロファイル（業務SSO）を使う
        return str(self._opt("user_data_dir") or "").strip()

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
        prof = self._profile_dir() or "ブラウザ既定（業務SSOを流用）"
        clip = "pyperclip 検出" if _has_pyperclip() else "pyperclip 無し（JS/tkinter/DOM で代替）"
        return True, (f"selenium 利用可（browser={self._browser()} / driver={drv} / profile={prof} / "
                      f"{clip}）。初回は headless=False で office.com にサインインしてください。")

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
        profile = self._profile_dir()
        if profile:  # 空なら付けない＝ブラウザ既定プロファイル（業務SSO）を使う
            opts.add_argument(f"--user-data-dir={profile}")
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

    # ---- セッション（ログイン→Researcher を開く。run 内で使い回す） -----------

    def _open_researcher(self, driver, emit=None) -> None:
        """office.com を開き、メニュー→エージェント入口 で Researcher に入る。案内タブを閉じる。"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        ready = int(self._opt("ready_timeout_ms")) / 1000
        login = int(self._opt("login_timeout_ms")) / 1000
        driver.get(self._opt("login_url"))
        self._emit(emit, type="progress", stage="se_login",
                   text="サインイン待ち（必要ならブラウザで完了してください）")
        # メニューボタンが押せるようになる＝サインイン済み。SSO/MFA を待てるよう長め。
        WebDriverWait(driver, login).until(
            EC.element_to_be_clickable((By.XPATH, self._opt("menu_button_xpath")))).click()
        self._emit(emit, type="progress", stage="se_agent",
                   text=f"{self._opt('agent_hint')} を開く")
        WebDriverWait(driver, ready).until(
            EC.element_to_be_clickable((By.XPATH, self._opt("agent_entrance_xpath")))).click()
        # 案内タブ/ブランディングを閉じる（環境により無いことも）
        close_sel = self._opt("close_tab_selector")
        if close_sel:
            try:
                WebDriverWait(driver, 60).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, close_sel))).click()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(int(self._opt("post_open_wait_ms")) / 1000)

    def _ensure_session(self, emit=None):
        """ドライバを（初回のみ）起動し、Researcher を開いて返す。以降は使い回す。"""
        if self._driver is not None:
            return self._driver
        self._emit(emit, type="progress", stage="se_launch", text=f"{self._browser()} 起動")
        driver = self._make_driver()
        try:
            self._open_researcher(driver, emit)
        except Exception:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._driver = driver
        return driver

    def _start_new_research(self, driver, emit=None) -> None:
        """2 章目以降: 新規リサーチを開始する（ボタンがあれば押す。無ければ入口から開き直す）。"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        sel = self._opt("new_chat_selector")
        if sel:
            try:
                WebDriverWait(driver, int(self._opt("ready_timeout_ms")) / 1000).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))).click()
                time.sleep(2)
                return
            except Exception:  # noqa: BLE001  フォールバックへ
                pass
        self._open_researcher(driver, emit)

    # ---- 入力・レポート長さ・続行 ------------------------------------------

    def _editor(self, driver):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(driver, int(self._opt("ready_timeout_ms")) / 1000).until(
            EC.presence_of_element_located((By.ID, self._opt("editor_id"))))

    def _type_and_submit(self, driver, editor, text: str) -> None:
        """複数行を途中送信せず入力（改行は Shift+Enter）→ 最後に Enter で送信。"""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        editor.click()
        for i, line in enumerate(text.split("\n")):
            if i:
                ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER)\
                    .key_up(Keys.SHIFT).perform()
            if line:
                editor.send_keys(line)
        editor.send_keys(Keys.ENTER)

    def _pick_length(self, driver, emit=None) -> None:
        """レポートの長さボタン（例「長い, 5 ページ以上」）が出れば選択（出なければスキップ）。"""
        sel = self._opt("length_selector")
        if not sel:
            return
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        to = int(self._opt("length_timeout_ms")) / 1000
        try:
            WebDriverWait(driver, to).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            driver.execute_script(
                "arguments[0].scrollIntoView();", driver.find_element(By.CSS_SELECTOR, sel))
            time.sleep(3)
            # スクロール後に要素が差し替わることがあるので取り直してからクリック
            WebDriverWait(driver, to).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))).click()
            self._emit(emit, type="progress", stage="se_length", text="レポート長さを選択")
        except Exception:  # noqa: BLE001  出ない構成もある
            self._emit(emit, type="progress", stage="se_length",
                       text="レポート長さの選択は表示されず（スキップ）")

    def _send_proceed(self, driver) -> None:
        """入力欄を全消去して続行の合図（既定 "go ahead"）を送る。"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        editor = driver.find_element(By.ID, self._opt("editor_id"))
        editor.click()
        editor.send_keys(Keys.CONTROL, "a")
        editor.send_keys(Keys.BACKSPACE)
        editor.send_keys(str(self._opt("proceed_text")), Keys.ENTER)

    # ---- 生成待ち（停止ボタンの出現→消滅で完了） ---------------------------

    def _wait_generation_done(self, driver, emit=None) -> None:
        """生成中(■=停止ボタン) → 完了(■が消える) を待つ。長時間ジョブ向けに堅牢化。"""
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        stop_sel = self._opt("stop_selector")
        poll = max(0.2, int(self._opt("poll_ms")) / 1000)
        refresh_every = int(self._opt("refresh_every_ms")) / 1000
        total = int(self._opt("timeout_ms")) / 1000

        start = time.monotonic()
        last_refresh = start
        last_beat = 0.0
        saw_stop = False
        while True:
            now = time.monotonic()
            if (now - start) > total:
                raise TimeoutError("生成完了を検出できませんでした（timeout_ms 超過）")
            try:
                stops = driver.find_elements(By.CSS_SELECTOR, stop_sel)
                visible = any(e.is_displayed() for e in stops)
                if visible:
                    saw_stop = True          # まだ生成中
                elif saw_stop:
                    return                    # ■→消失 ＝ 生成完了
            except StaleElementReferenceException:
                pass                          # DOM 差し替え中。次ループへ
            if (now - last_refresh) >= refresh_every:  # 動きが無ければリロード
                try:
                    driver.refresh()
                    WebDriverWait(driver, 30).until(
                        lambda d: d.execute_script("return document.readyState") == "complete")
                except Exception:  # noqa: BLE001
                    pass
                last_refresh = time.monotonic()
                saw_stop = False
            if emit and (now - last_beat) >= 15:
                self._emit(emit, type="progress", stage="se_wait",
                           text=f"リサーチ実行中… {int(now - start)}s"
                                + ("（生成中）" if saw_stop else "（開始待ち）"))
                last_beat = now
            time.sleep(poll)

    # ---- 応答取得（コピー→クリップボード。無ければ DOM） ------------------

    def _capture_answer(self, driver, emit=None) -> str:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        text = ""
        copy_sel = self._opt("copy_selector")
        if copy_sel:
            try:
                WebDriverWait(driver, 60).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, copy_sel))).click()
                time.sleep(1)
                text = self._read_clipboard(driver)
            except Exception:  # noqa: BLE001
                text = ""
        if not text and self._opt("answer_selector"):  # DOM フォールバック
            try:
                els = driver.find_elements(By.CSS_SELECTOR, self._opt("answer_selector"))
                text = els[-1].text if els else ""
            except Exception:  # noqa: BLE001
                text = ""
        return (text or "").strip()

    @staticmethod
    def _read_clipboard(driver) -> str:
        """クリップボード本文を取得（pyperclip → JS → tkinter の順に試す）。"""
        # 1) pyperclip（Windows で最も確実）
        try:
            import pyperclip
            v = pyperclip.paste()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        # 2) ブラウザの Clipboard API（許可・フォーカスが要る。取れれば確実に「今コピーした値」）
        try:
            v = driver.execute_async_script(
                "var cb=arguments[arguments.length-1];"
                "navigator.clipboard.readText().then(function(t){cb(t);})"
                ".catch(function(){cb('');});")
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        # 3) tkinter（GUI 環境）
        try:
            import tkinter
            r = tkinter.Tk()
            r.withdraw()
            v = r.clipboard_get()
            r.destroy()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ---- 1 章分のリサーチ ---------------------------------------------------

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None) -> ChapterResult:
        ok, msg = self.test()
        if not ok:
            return ChapterResult(ok=False, connector=self.kind, error=msg)
        t0 = time.time()
        try:
            driver = self._ensure_session(emit)
        except Exception as e:  # noqa: BLE001  セッション初期化失敗（次章のため破棄）
            self.close()
            return ChapterResult(ok=False, connector=self.kind, latency_sec=time.time() - t0,
                                 error=f"セッション初期化に失敗: {type(e).__name__}: {e}")
        try:
            if self._research_count > 0:              # 2 章目以降は新規リサーチ
                self._start_new_research(driver, emit)
            self._emit(emit, type="progress", stage="se_prompt", text="プロンプトを送信")
            editor = self._editor(driver)
            self._type_and_submit(driver, editor, prompt)
            self._pick_length(driver, emit)           # 長さ選択（あれば）
            if bool(self._opt("send_proceed")):       # 続行の合図（おまかせ）
                try:
                    self._send_proceed(driver)
                except Exception:  # noqa: BLE001  続行入力が不要な構成もある
                    pass
            self._emit(emit, type="progress", stage="se_wait", text="リサーチ生成を待機")
            self._wait_generation_done(driver, emit)
            self._emit(emit, type="progress", stage="se_copy", text="応答をコピー")
            text = self._capture_answer(driver, emit)
            self._research_count += 1
            return ChapterResult(
                text=text, citations=extract_citations(text), connector=self.kind,
                latency_sec=time.time() - t0, ok=bool(text),
                error="" if text else "応答本文を取得できませんでした"
                      "（copy_selector/クリップボード権限、または answer_selector を確認）")
        except Exception as e:  # noqa: BLE001  章単位の失敗（セッションは維持して次章へ）
            self._research_count += 1
            return ChapterResult(ok=False, connector=self.kind, latency_sec=time.time() - t0,
                                 error=f"{type(e).__name__}: {e}")

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None


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
