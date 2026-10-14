"""Selenium で Edge の M365 Copilot チャットを操作する作業担当。

実運用で動作確認済みの操作（jupyter-local-llm の m365_user_script.py）を土台に、
複数ウインドウで並列に使えるように次を変えている。

- ウインドウごとに別のユーザープロファイル（``--user-data-dir``）で Edge を起動する
  （同じプロファイルは同時に 1 つの Edge しか使えないため）。社内 SSO が通ればログインは自動
- 入力はクリップボードを使わず CDP の ``Input.insertText`` で入れる（長文でも速く、前面に出す必要が無い）
- 回答のコピーは ``clipboard_lock`` で全ウインドウ横断の順番待ちにし、取り違えを防ぐ
- 背後のウインドウが省電力で遅くならないよう起動オプションを付ける

セレクタは config の ``selectors`` で差し替えられる（UI 改定への対応はそこだけ）。
"""

from __future__ import annotations

import time

from .pool import BridgeError, Worker, WorkerPool

THROTTLE_FLAGS = [
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-features=msEdgeSleepingTabs,CalculateNativeWinOcclusion",
    "--no-first-run",
    "--no-default-browser-check",
]


class EdgeWorker(Worker):
    def __init__(self, index: int, cfg, clipboard_lock=None):
        super().__init__(index, cfg)
        self.driver = None
        self.clipboard_lock = clipboard_lock or WorkerPool.clipboard_lock
        self.sel = cfg.selectors

    # ---------------------------------------------------------------- 起動
    def start(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.service import Service

        profile = self.cfg.resolved_profile_dir() / self.id
        profile.mkdir(parents=True, exist_ok=True)
        opts = Options()
        opts.add_argument(f"--user-data-dir={profile}")
        for flag in THROTTLE_FLAGS:
            opts.add_argument(flag)
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.driver = webdriver.Edge(service=Service(self.cfg.driver_path), options=opts)
        try:
            x = (self.index * 60) % 600
            y = (self.index * 40) % 300
            self.driver.set_window_rect(x, y, self.cfg.window_width, self.cfg.window_height)
        except Exception:  # noqa: BLE001  位置調整は失敗しても構わない
            pass
        self.new_chat(timeout=self.cfg.login_timeout_sec)

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None

    # ---------------------------------------------------------------- 画面操作
    def _editor(self, timeout: float):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, self.sel["editor"])))

    def new_chat(self, timeout: float | None = None) -> None:
        """チャット画面を開き直して新しい会話にする。入力欄が出るまで待つ（SSO/手動ログイン込み）。"""
        self.driver.get(self.cfg.chat_url)
        try:
            self._editor(timeout or 60.0)
        except Exception as e:  # noqa: BLE001
            raise BridgeError(f"{self.id}: 入力欄が見つかりません（ログイン画面のままの可能性）: {e}") from e
        time.sleep(1.0)

    def _type(self, editor, text: str) -> None:
        """入力欄に本文を入れる。CDP insertText → だめなら send_keys（改行は Shift+Enter）。"""
        from selenium.webdriver.common.keys import Keys

        editor.click()
        try:
            self.driver.execute_cdp_cmd("Input.insertText", {"text": text})
            return
        except Exception:  # noqa: BLE001
            pass
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i:
                editor.send_keys(Keys.SHIFT, Keys.ENTER)
            if line:
                editor.send_keys(line)

    def _count(self, selector: str) -> int:
        from selenium.webdriver.common.by import By

        return len(self.driver.find_elements(By.CSS_SELECTOR, selector))

    def _wait_done(self, copy_count_before: int, timeout: float) -> None:
        """停止ボタンが「出た→消えた」になるまで待つ。出ないまま新しいコピーボタンが増えたら完了扱い。"""
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.common.by import By

        start = time.monotonic()
        saw_stop = False
        while True:
            if time.monotonic() - start > timeout:
                raise BridgeError(f"{self.id}: {timeout:.0f} 秒以内に回答が終わりませんでした")
            try:
                visible = [e for e in self.driver.find_elements(By.CSS_SELECTOR, self.sel["stop_button"])
                           if e.is_displayed()]
                if visible:
                    saw_stop = True
                elif saw_stop:
                    return
                elif self._count(self.sel["copy_button"]) > copy_count_before:
                    # 停止ボタンを見ずにコピーボタンが増えた（短い回答で見逃した等）。
                    # 送った自分の発言分で増えただけの可能性があるので、少し待って停止ボタンが
                    # 出ていないことを再確認してから完了とみなす
                    time.sleep(2.0)
                    if not any(e.is_displayed() for e in
                               self.driver.find_elements(By.CSS_SELECTOR, self.sel["stop_button"])):
                        return
                    saw_stop = True
            except StaleElementReferenceException:
                pass
            time.sleep(0.5)

    def _read_last_reply(self) -> str:
        """最後の回答本文を取る。コピーボタン（Markdown が保たれる）→ だめなら DOM の文字。"""
        from selenium.webdriver.common.by import By

        buttons = self.driver.find_elements(By.CSS_SELECTOR, self.sel["copy_button"])
        if buttons:
            with self.clipboard_lock:
                _clipboard_write("")
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView();", buttons[-1])
                    buttons[-1].click()
                except Exception:  # noqa: BLE001
                    pass
                else:
                    for _ in range(20):
                        time.sleep(0.25)
                        v = _clipboard_read(self.driver)
                        if v:
                            return v
        nodes = self.driver.find_elements(By.CSS_SELECTOR, self.sel["response"])
        if nodes:
            return nodes[-1].get_attribute("innerText") or nodes[-1].text or ""
        return ""

    def send(self, text: str) -> str:
        if self.driver is None:
            raise BridgeError(f"{self.id}: ブラウザが起動していません")
        from selenium.webdriver.common.keys import Keys

        editor = self._editor(30.0)
        before = self._count(self.sel["copy_button"])
        self._type(editor, text)
        time.sleep(0.3)
        editor.send_keys(Keys.ENTER)
        self._wait_done(before, self.cfg.reply_timeout_sec)
        time.sleep(1.0)
        reply = (self._read_last_reply() or "").strip()
        self.turns += 1
        if not reply:
            raise BridgeError(f"{self.id}: 回答を読み取れませんでした（コピーボタン/DOM とも空）")
        for phrase in self.cfg.error_phrases:
            if phrase and phrase in reply:
                self.error = phrase
                raise BridgeError(f"{self.id}: Copilot が制限・障害を返しました（{phrase}）")
        self.error = ""
        return reply


# ---------------------------------------------------------------- クリップボード

def _clipboard_write(value: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(value)
    except Exception:  # noqa: BLE001
        pass


def _clipboard_read(driver) -> str:
    try:
        import pyperclip

        v = pyperclip.paste()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    try:
        v = driver.execute_async_script(
            "var cb=arguments[arguments.length-1];"
            "navigator.clipboard.readText().then(function(t){cb(t);})"
            ".catch(function(){cb('');});")
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    try:
        import tkinter

        r = tkinter.Tk()
        r.withdraw()
        try:
            return r.clipboard_get() or ""
        finally:
            r.destroy()
    except Exception:  # noqa: BLE001
        return ""
