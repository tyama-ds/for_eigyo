"""M365 Copilot（リサーチエージェント）への接続アダプタ。

Microsoft 365 Copilot の「Researcher（リサーチ）」エージェントに、章ごとの調査プロンプトを
投げて結果を受け取るための **差し替え可能なコネクタ** 群。上位の擬似GEPAリサーチ
（``copilotresearch.py``）はこのインタフェースだけに依存する。

コネクタの種類（``kind``）:

- ``demo``   : LLM も M365 も不要のモック。プロンプト（＝進化した指示）が「出典」「数値」
               「比較」「最新」等を含むほど濃い回答を返すので、擬似GEPAの改善曲線を体験できる。
- ``bridge`` : 人手ブリッジ。各章の完成プロンプトを UI に表示 → 人が M365 Copilot に貼り付け、
               返ってきた回答を貼り戻す。自動化/API が塞がれた社内環境でも確実に動く既定手段。
- ``selenium`` : **ユーザー実証済みスクリプトの忠実移植**。Edge を Selenium で駆動し、
               office.com → Researcher → プロンプト送信 → 「長い」選択 → go ahead →
               停止ボタンの出現→消滅で完了検知 → 応答コピー、を章ごとに 1 回転する。
               msedgedriver の場所は ``driver_path`` で指定（既定はスクリプトのパス）。
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
                 emit: EmitFn | None = None, ask_bridge: AskBridgeFn | None = None,
                 should_cancel=None) -> ChapterResult:
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

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None,
                 should_cancel=None) -> ChapterResult:
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

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None,
                 should_cancel=None) -> ChapterResult:
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
# selenium — ユーザー実証済みスクリプトの忠実移植（Edge で M365 Copilot Researcher を駆動）
# ---------------------------------------------------------------------------

class SeleniumConnector(BaseConnector):
    r"""実運用で動作確認済みのスクラッチスクリプトを **そのまま移植** したコネクタ。

    1 回のリサーチはスクリプト原文の手順:

      _login()（Edge 起動 → office.com → sleep(1)）
      → メニュー click → リサーチツール入口 click → タブを閉じる（WebDriverWait 60）
      → sleep(10) → 入力欄へ send_keys(プロンプト, ENTER)
      → 「長い, 5 ページ以上」を scrollIntoView → sleep(5) → 再定義 → click
      → 入力欄 全選択 → 削除 → "go ahead" + ENTER（おまかせ）
      → wait_until_generation_done（停止ボタン■の出現→消滅。1秒ポーリング・10分毎 refresh）
      → 「応答のコピー」click → クリップボードから本文取得

    章のまたぎ方（``session_mode``）:

    - ``same_chat``（既定）: **同一チャットでリサーチを繰り返す**。2 章目以降はログイン・
      画面遷移を省略して同じ入力欄へ続きのプロンプトを送る（Researcher が前章の文脈を
      引き継ぐ）。同一チャットでは「応答のコピー」が応答の数だけ並ぶため、**送信前の
      ボタン数を数えて増えた最後の 1 個**を押す。「長い」の質問が出た時だけ go ahead を送る。
      ブラウザは run 終了時（close()）に閉じる。※擬似GEPAで同じ章を聞き直すと文脈が
      混ざるので、予算 0〜1 推奨。
    - ``per_chapter``: スクリプト原文どおり、章ごとにブラウザを起動し直す使い切り。

    移植にあたっての追加は: クリップボード読み取り / 例外の ChapterResult 化 /
    プロンプト改行の 1 行化 / 上記 same_chat の継続処理、のみ。

    options（既定はスクリプトの値そのまま。tenant/言語が違う時だけ上書き）:
      session_mode      : "same_chat"（既定・同一チャット継続）/ "per_chapter"（原文どおり使い切り）
      driver_path       : msedgedriver の場所（既定 C:\Driver\edgedriver_win32\msedgedriver.exe。
                          環境変数 EDGEDRIVER_PATH / MSEDGEDRIVER_PATH でも指定可）
      login_url         : https://www.office.com/?auth=2
      menu_button_xpath / agent_entrance_xpath / close_tab_selector : Researcher への導線
      editor_id         : m365-chat-editor-target-element
      length_selector   : button[aria-label='長い, 5 ページ以上']（空で選択をスキップ）
      length_timeout_sec: 「長い」の出現待ち秒（スクリプト既定 50。同一チャットの 2 章目以降で
                          質問が出ないテナントでは短くすると速い）
      proceed_text      : "go ahead"（「長い」を選んだ時だけ送る）
      stop_selector     : button[aria-label='生成を停止する']
      copy_selector     : button[aria-label='応答のコピー']
      poll_sec / refresh_every_sec / total_timeout_sec : 生成待ち（スクリプト既定 1 / 600 / None=無制限）
    """

    kind = "selenium"
    label = "Selenium（実証スクリプト移植・Edge）"

    DEFAULTS = {
        "session_mode": "same_chat",
        "driver_path": r"C:\Driver\edgedriver_win32\msedgedriver.exe",
        "login_url": "https://www.office.com/?auth=2",
        "menu_button_xpath":
            "/html/body/div[1]/div/div/main/div[1]/div/header/div/div[1]/button",
        "agent_entrance_xpath":
            "/html/body/div[1]/div/div/main/div[1]/div/div/div[2]/div/div[1]/div[2]"
            "/div[1]/div/button[1]/span[2]",
        "close_tab_selector": "button[data-testid='header-branding-container']",
        "editor_id": "m365-chat-editor-target-element",
        "length_selector": "button[aria-label='長い, 5 ページ以上']",
        "length_timeout_sec": 50,
        "proceed_text": "go ahead",
        "stop_selector": "button[aria-label='生成を停止する']",
        "copy_selector": "button[aria-label='応答のコピー']",
        "poll_sec": 1,
        "refresh_every_sec": 600,
        "total_timeout_sec": None,
    }

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        self._driver = None  # same_chat モードで run 内に使い回すセッション

    def _opt(self, key):
        return self.options.get(key, self.DEFAULTS.get(key))

    def _mode(self) -> str:
        return str(self._opt("session_mode") or "same_chat").lower()

    def _driver_path(self) -> str:
        import os

        v = self.options.get("driver_path")
        if v:
            return str(v).strip()
        return (os.environ.get("EDGEDRIVER_PATH") or os.environ.get("MSEDGEDRIVER_PATH")
                or str(self.DEFAULTS["driver_path"]))

    def test(self) -> tuple[bool, str]:
        try:
            import selenium  # noqa: F401
        except ImportError:
            return False, "selenium が未インストールです（`pip install selenium`）"
        import os

        dp = self._driver_path()
        if not os.path.exists(dp):
            return False, f"driver_path が見つかりません: {dp}"
        clip = "pyperclip 検出" if _has_pyperclip() else \
            "pyperclip 無し（`pip install pyperclip` 推奨）"
        return True, f"selenium 利用可（Edge / driver={dp} / {clip}）"

    # ---- 以下、スクリプト原文の移植（ロジック・待ち時間は原文どおり） ----------

    def _login(self):
        """M365へログイン（原文 _login の移植）。"""
        from selenium import webdriver
        from selenium.webdriver.edge.service import Service

        login_url = self._opt("login_url")
        driver = webdriver.Edge(service=Service(self._driver_path()))
        driver.get(login_url)
        # 3秒待機
        time.sleep(1)
        return driver

    def _wait_until_generation_done(self, driver, should_cancel=None):
        """生成中(■)→ 完了(⇒)に戻るまで待つ（原文 wait_until_generation_done の移植）。

        1秒おきに確認。10分見つからない/止まらないなら refresh。
        Returns: True（完了を検知）
        """
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        stop_button = (By.CSS_SELECTOR, self._opt("stop_selector"))
        poll_sec = float(self._opt("poll_sec") or 1)
        refresh_every_sec = float(self._opt("refresh_every_sec") or 600)
        raw = self._opt("total_timeout_sec")
        total_timeout_sec = float(raw) if raw not in (None, "", 0, "0") else None

        start = time.monotonic()
        last_refresh = start
        saw_stop = False  # 一度でも■を見たか

        while True:
            now = time.monotonic()

            if total_timeout_sec is not None and (now - start) > total_timeout_sec:
                raise TimeoutError("生成完了を検出できませんでした。")
            if should_cancel and should_cancel():  # 移植追加: アプリのキャンセルに応答
                raise RuntimeError("キャンセルされました")

            try:
                stops = driver.find_elements(*stop_button)
                visible = [e for e in stops if e.is_displayed()]

                if visible:
                    # ■が表示中＝まだ生成中
                    saw_stop = True
                else:
                    # ■が消えた
                    if saw_stop:
                        # 「■→消失」を検知 ＝ 生成完了
                        return True
                    # まだ■を一度も見てない＝送信直後でDOM未反映の可能性
                    # → そのまま待機継続

            except StaleElementReferenceException:
                # DOM差し替え中。次ループへ
                pass

            # 一定時間動きなしなら refresh
            if (now - last_refresh) >= refresh_every_sec:
                driver.refresh()
                WebDriverWait(driver, 30).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                last_refresh = time.monotonic()
                saw_stop = False  # リフレッシュ後はリセット

            time.sleep(poll_sec)

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None,
                 should_cancel=None) -> ChapterResult:
        ok, msg = self.test()
        if not ok:
            return ChapterResult(ok=False, connector=self.kind, error=msg)
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        # 入力欄では Enter=送信のため、章プロンプトは 1 行へ潰して送る（移植上の必要調整）
        research_prompt = " ".join(s.strip() for s in str(prompt).splitlines() if s.strip())
        same_chat = self._mode() == "same_chat"
        copy_sel = self._opt("copy_selector")
        t0 = time.time()
        driver = None
        try:
            # --- セッション: same_chat は 2 章目以降を同じチャットで続ける ---------
            if same_chat and self._driver is not None:
                driver = self._driver
                try:
                    _ = driver.current_url  # 生存確認（閉じられていたら作り直す）
                    self._emit(emit, type="progress", stage="se_agent",
                               text="同一チャットで続きのリサーチ")
                except Exception:  # noqa: BLE001
                    self.close()
                    driver = None
            if driver is None:
                # ログインして
                self._emit(emit, type="progress", stage="se_login", text="Edge 起動 → office.com")
                driver = self._login()
                driver.find_element(By.XPATH, self._opt("menu_button_xpath")).click()
                _agent_entrance = driver.find_element(By.XPATH, self._opt("agent_entrance_xpath"))

                # リサーチツールをクリックして移動。
                _agent_entrance.click()
                self._emit(emit, type="progress", stage="se_agent", text="リサーチツールへ移動")

                btn = WebDriverWait(driver, 60).until(EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, self._opt("close_tab_selector"))))  # タブを閉じる
                btn.click()

                time.sleep(10)
                if same_chat:
                    self._driver = driver

            # 同一チャットでは「応答のコピー」が応答の数だけ並ぶ。
            # 送信前の個数を控え、増えた最後の 1 個（＝今回の応答）を押す。
            n_copy_before = len(driver.find_elements(By.CSS_SELECTOR, copy_sel))

            self._emit(emit, type="progress", stage="se_prompt", text="プロンプトを送信")
            driver.find_element(By.ID, self._opt("editor_id")).send_keys(
                research_prompt, Keys.ENTER)

            # レポートの長い方を選ぶ。下へ移動してクリック。
            # （同一チャットの 2 章目以降では質問が出ないことがある → 出た時だけ）
            length_sel = self._opt("length_selector")
            length_to = float(self._opt("length_timeout_sec") or 50)
            clicked_length = False
            if length_sel:
                try:
                    elem = WebDriverWait(driver, length_to).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, length_sel)))
                    driver.execute_script("arguments[0].scrollIntoView();", elem)
                    time.sleep(5)
                    elem = WebDriverWait(driver, length_to).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, length_sel)))
                    # よくわからないけど、ここで再定義必要みたい。
                    elem.click()
                    clicked_length = True
                except Exception:  # noqa: BLE001  質問が出ない場合はそのまま生成が走る
                    self._emit(emit, type="progress", stage="se_length",
                               text="長さの質問は表示されず（そのまま生成待ちへ）")

            if clicked_length:  # 「長い」を選んだ時だけ続行の合図を送る
                elem = driver.find_element(By.ID, self._opt("editor_id"))
                elem.click()

                # 全選択 → 削除
                elem.send_keys(Keys.CONTROL, "a")
                elem.send_keys(Keys.BACKSPACE)

                elem.send_keys(str(self._opt("proceed_text")), Keys.ENTER)  # これだとおまかせ

            # 生成完了まで待つ
            self._emit(emit, type="progress", stage="se_wait",
                       text="生成完了を待機（■の出現→消滅を1秒ポーリング）")
            self._wait_until_generation_done(driver, should_cancel)
            self._emit(emit, type="progress", stage="se_copy", text="✅ 生成完了！ → 応答のコピー")

            # ① 今回の応答のコピーが増えるまで待つ ② 最後の 1 個をクリック
            WebDriverWait(driver, 60).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, copy_sel)) > n_copy_before)
            btns = [e for e in driver.find_elements(By.CSS_SELECTOR, copy_sel)
                    if e.is_displayed()] or driver.find_elements(By.CSS_SELECTOR, copy_sel)
            btns[-1].click()
            time.sleep(1)
            text = (self._read_clipboard(driver) or "").strip()  # 移植追加: 本文の取得

            return ChapterResult(
                text=text, citations=extract_citations(text), connector=self.kind,
                latency_sec=time.time() - t0, ok=bool(text),
                error="" if text else
                "クリップボードから本文を取得できませんでした（`pip install pyperclip` を確認）")
        except Exception as e:  # noqa: BLE001  失敗は UI に返す（アプリは落とさない）
            if same_chat:  # 失敗した章のセッションは破棄 → 次章は新しいチャットでやり直す
                self.close()
                driver = None
            return ChapterResult(ok=False, connector=self.kind, latency_sec=time.time() - t0,
                                 error=f"{type(e).__name__}: {e}")
        finally:
            if not same_chat:  # per_chapter はスクリプト原文どおり使い切り
                try:
                    if driver:
                        driver.quit()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        """same_chat モードのセッションを閉じる（run 終了時にオーケストレータから呼ばれる）。"""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None

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
        # 2) ブラウザの Clipboard API（許可・フォーカスが要る）
        try:
            v = driver.execute_async_script(
                "var cb=arguments[arguments.length-1];"
                "navigator.clipboard.readText().then(function(t){cb(t);})"
                ".catch(function(){cb('');});")
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        # 3) tkinter（GUI 環境）。root は必ず destroy する
        try:
            import tkinter
            r = tkinter.Tk()
            r.withdraw()
            try:
                v = r.clipboard_get()
            finally:
                r.destroy()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        return ""


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

    def research(self, prompt, *, meta=None, emit=None, ask_bridge=None,
                 should_cancel=None) -> ChapterResult:
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
