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

from .pool import BridgeError, ModelStalled, Worker, WorkerPool, WorkerRestarted

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
        self.launch_mode = ""
        self.chat_turns = 0   # 今の Copilot 会話で送った回数（削除要否の判定用）
        self.current_model = ""   # 画面で選んだモデルの表示名
        self.clipboard_lock = clipboard_lock or WorkerPool.clipboard_lock
        self.sel = cfg.selectors

    # ---------------------------------------------------------------- 起動
    def _launch(self, mode: str):
        """Edge を起動する。mode: separate（専用プロファイル＋起動オプション）/ temp（プロファイル指定なし、
        起動オプションあり）/ bare（実績スクリプトと同じ素の起動）。"""
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.service import Service

        # manager: driver_path を使わず Selenium Manager に Edge の版に合う msedgedriver を取らせる
        service = None if mode == "manager" else Service(self.cfg.driver_path)
        if mode == "bare":
            return webdriver.Edge(service=service)
        opts = Options()
        if mode == "separate":
            profile = self.cfg.resolved_profile_dir() / self.id
            profile.mkdir(parents=True, exist_ok=True)
            opts.add_argument(f"--user-data-dir={profile}")
        for flag in THROTTLE_FLAGS:
            opts.add_argument(flag)
        if self.cfg.headless:
            opts.add_argument("--headless=new")
            opts.add_argument(f"--window-size={self.cfg.window_width},{self.cfg.window_height}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        return webdriver.Edge(service=service, options=opts)

    def start(self) -> None:
        # 社内 PC ではポリシーでプロファイルの場所が固定され、--user-data-dir 指定だと Edge が即終了する
        # ことがある（"Chrome instance exited"）。profile_mode=auto なら separate → temp → bare の順に試す
        modes = (["separate", "temp", "bare", "manager"] if self.cfg.profile_mode == "auto"
                 else [self.cfg.profile_mode])
        if self.cfg.headless:
            # bare は起動オプションを付けないので headless にならない
            modes = [m for m in modes if m != "bare"] or ["separate"]
            if self.cfg.read_mode == "clipboard":
                self.cfg.read_mode = "auto"
                self._log("headless のため read_mode を auto（画面の要素→だめならクリップボード）にします。"
                          "確実にするには config で read_mode: dom を指定してください")
        last: Exception | None = None
        for mode in modes:
            try:
                self.driver = self._launch(mode)
                self.launch_mode = mode
                break
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                hint = ""
                if mode == "separate" and "exited" in msg:
                    hint = ("（同じプロファイルを使う Edge が既に開いていませんか? サーバと probe の同時実行や、"
                            "前回のウインドウの閉じ忘れが原因になります）")
                print(f"[{self.id}] 起動失敗（{mode}）: {msg}{hint} → 次の方式を試します", flush=True)
        else:
            raise BridgeError(f"{self.id}: Edge を起動できません（{modes} を試行）: {last}") from last
        if self.launch_mode != "separate":
            print(f"[{self.id}] 起動方式: {self.launch_mode}（ウインドウごとの専用プロファイルは使えないため、"
                  "一時プロファイルで動作。ログインは毎回 SSO 経由）", flush=True)
        self._log_versions()
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
                if self.cfg.delete_chat_on_close and self.chat_turns > 0:
                    try:
                        self.delete_current_chat()
                    except Exception:  # noqa: BLE001
                        pass
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.driver = None

    def _log_versions(self) -> None:
        """Edge と msedgedriver の版を表示し、ずれていれば警告する（社内配布で Edge だけ更新されることがある）。"""
        try:
            caps = self.driver.capabilities or {}
            browser = str(caps.get("browserVersion") or "?")
            drv = str((caps.get("msedge") or caps.get("chrome") or {}).get("msedgedriverVersion")
                      or (caps.get("msedge") or caps.get("chrome") or {}).get("chromedriverVersion") or "?")
            drv_short = drv.split(" ")[0]
            self._log(f"Edge {browser} / msedgedriver {drv_short}")
            if browser.split(".")[0] != drv_short.split(".")[0] and "?" not in (browser, drv_short):
                self._log("警告: Edge と msedgedriver の大版が違います。接続切れの原因になります。"
                          "https://developer.microsoft.com/microsoft-edge/tools/webdriver/ から Edge と同じ版を"
                          "取得するか、--profile-mode manager で自動取得を試してください")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 画面操作
    def _editor(self, timeout: float):
        """入力欄を探す。メイン文書に無ければ iframe の中も順に探し、見つかった枠に切り替えたまま返す。"""
        from selenium.webdriver.common.by import By

        deadline = time.monotonic() + timeout
        last_err = "見つからない"
        while True:
            try:
                self.driver.switch_to.default_content()
                el = self._find_editor_here()
                if el is not None:
                    return el
                frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe")
                for i, fr in enumerate(frames):
                    try:
                        self.driver.switch_to.default_content()
                        self.driver.switch_to.frame(fr)
                        el = self._find_editor_here()
                        if el is not None:
                            if not getattr(self, "_in_frame_logged", False):
                                self._log(f"入力欄は iframe[{i}] の中にあります（枠に切り替えて操作）")
                                self._in_frame_logged = True
                            return el
                    except Exception as e:  # noqa: BLE001
                        last_err = str(e).splitlines()[0] if str(e) else type(e).__name__
                        if _is_disconnect(last_err):
                            raise
                self.driver.switch_to.default_content()
            except Exception as e:  # noqa: BLE001
                last_err = str(e).splitlines()[0] if str(e) else type(e).__name__
                if _is_disconnect(last_err):
                    raise
            if time.monotonic() > deadline:
                raise TimeoutError(f"入力欄 {self.sel['editor']} が {timeout:.0f} 秒以内に見つかりません（{last_err}）")
            time.sleep(1.0)

    def _find_editor_here(self):
        from selenium.webdriver.common.by import By

        for el in self.driver.find_elements(By.CSS_SELECTOR, self.sel["editor"]):
            try:
                if el.is_displayed() and el.is_enabled():
                    return el
            except Exception:  # noqa: BLE001
                continue
        return None

    # ---------------------------------------------------------------- 生存確認・再起動
    def _alive(self) -> tuple[bool, str]:
        """driver と Edge の接続が生きているか。"""
        if self.driver is None:
            return False, "driver なし"
        try:
            self.driver.window_handles
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, (str(e).splitlines()[0] if str(e) else type(e).__name__)

    def _restart(self, reason: str) -> None:
        """接続が切れた Edge を閉じて起動し直す。Copilot 側の会話は失われる。"""
        self._log(f"ブラウザとの接続が切れました（{reason}）。Edge を起動し直します")
        self.close()
        self.start()

    def _focus_chat_window(self) -> None:
        """タブ/ウインドウが複数あれば Copilot のタブへ切り替える（SSO 経由で増えることがある）。"""
        handles = self.driver.window_handles
        if len(handles) <= 1:
            return
        host = self.cfg.chat_url.split("/")[2] if "//" in self.cfg.chat_url else ""
        best = None
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                url = self.driver.current_url
            except Exception:  # noqa: BLE001
                continue
            if host and host in url:
                best = h
        if best is None:
            best = handles[-1]
        self.driver.switch_to.window(best)
        self._log(f"タブが {len(handles)} 個あるため Copilot のタブへ切り替え: {self.driver.current_url}")

    def _log(self, msg: str) -> None:
        print(f"[{self.id}] {msg}", flush=True)

    def _find(self, selector: str, timeout: float = 5.0):
        """先頭が // なら XPath、それ以外は CSS で、表示されている要素を 1 つ返す（無ければ None）。"""
        from selenium.webdriver.common.by import By

        by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
        deadline = time.monotonic() + timeout
        while True:
            for el in self.driver.find_elements(by, selector):
                try:
                    if el.is_displayed():
                        return el
                except Exception:  # noqa: BLE001
                    continue
            if time.monotonic() > deadline:
                return None
            time.sleep(0.3)

    def delete_current_chat(self) -> tuple[bool, str]:
        """今の会話を Copilot の履歴から削除する（画面操作。セレクタは config.selectors で調整）。

        手順: 会話一覧の行（chat_item）にホバー → 行の中の「…」（chat_menu_button、行に対する相対 XPath）
        → メニューの「削除」（delete_menu_item）→ 確認ダイアログの「削除」（confirm_delete、出なければ省略）。
        """
        if self.driver is None or self.chat_turns == 0:
            return False, "削除する会話がない"
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By

        try:
            self.driver.switch_to.default_content()
            item = self._find(self.sel.get("chat_item", ""), 3.0) if self.sel.get("chat_item") else None
            if item is None and self.sel.get("history_button"):
                # 会話一覧が畳まれていれば開いてから探し直す
                btn = self._find(self.sel["history_button"], 2.0)
                if btn is not None:
                    btn.click()
                    time.sleep(0.8)
                    item = self._find(self.sel["chat_item"], 3.0)
            if item is None:
                return False, "会話一覧の行（selectors.chat_item）が見つからない"
            ActionChains(self.driver).move_to_element(item).pause(0.5).perform()
            rel = self.sel.get("chat_menu_button", ".//button[last()]")
            menu_btn = None
            for el in item.find_elements(By.XPATH if rel.startswith(".") else By.CSS_SELECTOR, rel):
                try:
                    if el.is_displayed():
                        menu_btn = el
                except Exception:  # noqa: BLE001
                    continue
            if menu_btn is None:
                return False, "行の中の「…」ボタン（selectors.chat_menu_button）が見つからない"
            menu_btn.click()
            time.sleep(0.8)
            del_item = self._find(self.sel.get("delete_menu_item", ""), 5.0)
            if del_item is None:
                return False, "メニューの削除項目（selectors.delete_menu_item）が見つからない"
            del_item.click()
            time.sleep(0.8)
            confirm = self._find(self.sel.get("confirm_delete", ""), 3.0) if self.sel.get("confirm_delete") else None
            if confirm is not None:
                confirm.click()
                time.sleep(0.8)
            self.chat_turns = 0
            self._log("会話を削除しました")
            return True, ""
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            if _is_disconnect(msg):
                raise
            return False, msg

    def select_model(self, label: str) -> tuple[bool, str]:
        """画面のモデル切り替えで label を含む項目を選ぶ（selectors.model_button / model_item）。"""
        if not label:
            return True, ""
        try:
            self.driver.switch_to.default_content()
            btn = self._find(self.sel.get("model_button", ""), 5.0) if self.sel.get("model_button") else None
            if btn is None:
                return False, "モデル選択ボタン（selectors.model_button）が見つからない"
            btn.click()
            time.sleep(0.8)
            item = self._find(self.sel.get("model_item", "").replace("{label}", label), 5.0)
            if item is None:
                return False, f"モデル項目「{label}」（selectors.model_item）が見つからない"
            item.click()
            time.sleep(0.8)
            self.current_model = label
            self._log(f"モデルを「{label}」に切り替えました")
            return True, ""
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            if _is_disconnect(msg):
                raise
            return False, msg

    def new_chat(self, timeout: float | None = None, model: str | None = None) -> None:
        """チャット画面を開き直して新しい会話にする。入力欄が出るまで待つ（SSO/手動ログイン込み）。
        model（画面の表示名）があれば、開いた後にモデルを切り替える。"""
        ok, reason = self._alive()
        if not ok:
            # 接続が切れていれば起動し直す（start() が new_chat() を呼ぶのでここで戻る）
            self._restart(reason)
            if model:
                self.select_model(model)
            return
        if self.cfg.delete_chat_on_close and self.chat_turns > 0:
            done, why = self.delete_current_chat()
            if not done:
                self._log(f"会話を削除できませんでした: {why}")
        self.chat_turns = 0
        self.driver.get(self.cfg.chat_url)
        self._focus_chat_window()
        try:
            self._editor(timeout or 60.0)
        except Exception as e:  # noqa: BLE001
            raise BridgeError(f"{self.id}: 入力欄が見つかりません（ログイン画面のままの可能性）: "
                              f"url={_safe_url(self.driver)} {e}") from e
        # 読み込み直後にページが自分でタブを開き直すことがあるので、少し待ってもう一度 Copilot のタブへ寄せる
        time.sleep(2.0)
        self._focus_chat_window()
        if model:
            done, why = self.select_model(model)
            if not done:
                self._log(f"モデルを切り替えられませんでした（{why}）。今のモデルのまま続けます")
        try:
            n = len(self.driver.window_handles)
        except Exception:  # noqa: BLE001
            n = -1
        self._log(f"チャット画面の準備完了 tabs={n} url={_safe_url(self.driver)}")

    # 入力方式。社内 Edge では DevTools 関連のポリシーで CDP（Input.insertText）が接続切れを招くことが
    # あるため、既定（auto）は CDP を使わない: js → paste → keys の順
    INPUT_ORDER = {"auto": ["js", "paste", "keys"], "js": ["js"], "paste": ["paste"], "keys": ["keys"],
                   "cdp": ["cdp", "js", "paste", "keys"]}

    def _type(self, editor, text: str) -> str:
        """入力欄に本文を入れる。成功した方式名を返す。"""
        editor.click()
        errors = []
        for mode in self.INPUT_ORDER.get(self.cfg.input_mode, self.INPUT_ORDER["auto"]):
            try:
                getattr(self, f"_type_{mode}")(editor, text)
                ok, why = self._editor_text_ok(editor, text)
                if not ok:
                    errors.append(f"{mode}: {why}")
                    self._log(f"入力方式 {mode} は不可（{why}）→ 次の方式を試します")
                    self._clear_editor(editor)
                    continue
                return mode
            except Exception as e:  # noqa: BLE001
                msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                if _is_disconnect(msg):
                    raise
                errors.append(f"{mode}: {msg}")
        raise BridgeError(f"{self.id}: 入力欄に文字を入れられません（{' / '.join(errors)}）")

    def _editor_text(self, editor) -> str | None:
        try:
            return self.driver.execute_script(
                "var e=arguments[0]; return (e.innerText||e.textContent||e.value||'');", editor)
        except Exception:  # noqa: BLE001
            return None

    def _editor_text_ok(self, editor, text: str) -> tuple[bool, str]:
        """入力欄に本文が入ったか。日本語などが ? に化けていないかも確かめる（send_keys で起きることがある）。"""
        got = self._editor_text(editor)
        if got is None:
            return True, ""  # 確認できなければ入ったものとして進む
        if not got.strip():
            return False, "入力欄が空のまま"
        sample = [ch for ch in text if ord(ch) > 127][:8]
        if sample and not any(ch in got for ch in sample):
            return False, "日本語などが入力欄に入っていない（? に化けた可能性）"
        return True, ""

    def _clear_editor(self, editor) -> None:
        from selenium.webdriver.common.keys import Keys

        try:
            editor.click()
            editor.send_keys(Keys.CONTROL, "a")
            editor.send_keys(Keys.BACKSPACE)
        except Exception:  # noqa: BLE001
            pass

    def _type_js(self, editor, text: str) -> None:
        """execCommand('insertText'): contenteditable に対して beforeinput/input を発生させる標準の経路。"""
        ok = self.driver.execute_script(
            "var e=arguments[0], t=arguments[1]; e.focus();"
            "try{return document.execCommand('insertText', false, t);}catch(err){return false;}",
            editor, text)
        if not ok:
            raise RuntimeError("execCommand が false")

    def _type_paste(self, editor, text: str) -> None:
        """クリップボード経由（Ctrl+V）。クリップボードは PC で 1 つなので排他する。"""
        from selenium.webdriver.common.keys import Keys

        with self.clipboard_lock:
            if not _clipboard_write(text):
                raise RuntimeError("クリップボードに書けない（pyperclip なし）")
            editor.click()
            editor.send_keys(Keys.CONTROL, "v")
            time.sleep(0.5)

    def _type_keys(self, editor, text: str) -> None:
        """1 行ずつ send_keys（改行は Shift+Enter）。遅いが確実。"""
        from selenium.webdriver.common.keys import Keys

        for i, line in enumerate(text.split("\n")):
            if i:
                editor.send_keys(Keys.SHIFT, Keys.ENTER)
            if line:
                editor.send_keys(line)

    def _type_cdp(self, editor, text: str) -> None:
        self.driver.execute_cdp_cmd("Input.insertText", {"text": text})

    def _count(self, selector: str) -> int:
        from selenium.webdriver.common.by import By

        return len(self.driver.find_elements(By.CSS_SELECTOR, selector))

    def _wait_done(self, copy_count_before: int, timeout: float) -> None:
        """停止ボタンが「出た→消えた」になるまで待つ。出ないまま新しいコピーボタンが増えたら完了扱い。"""
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.common.by import By

        start = time.monotonic()
        saw_stop = False
        start_limit = float(self.cfg.start_timeout_sec or 0)
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ModelStalled(f"{self.id}: {timeout:.0f} 秒以内に回答が終わりませんでした")
            if start_limit and not saw_stop and elapsed > start_limit:
                raise ModelStalled(f"{self.id}: {start_limit:.0f} 秒たっても生成が始まりません（モデルが詰まった可能性）")
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

        mode = self.cfg.read_mode
        if mode in ("dom", "auto"):
            # 画面の回答要素から直接読む（クリップボードを使わない）。auto は要素が見つかったときだけ
            text = self._read_last_reply_dom()
            if text or mode == "dom":
                return text
        buttons = self.driver.find_elements(By.CSS_SELECTOR, self.sel["copy_button"])
        if buttons:
            with self.clipboard_lock:
                # 利用者のクリップボード（文字列）を退避し、読み終えたら戻す
                saved = _clipboard_read(self.driver)
                _clipboard_write("")
                try:
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
                finally:
                    if saved:
                        _clipboard_write(saved)
        return self._read_last_reply_dom()

    def _read_last_reply_dom(self) -> str:
        """画面の回答本文を読む。selectors.response で見つからなければ、最後の「応答のコピー」ボタンの
        祖先をたどって本文を含む要素を探す（セレクタが分からなくても読めるようにする）。"""
        from selenium.webdriver.common.by import By

        nodes = self.driver.find_elements(By.CSS_SELECTOR, self.sel["response"])
        if nodes:
            return nodes[-1].get_attribute("innerText") or nodes[-1].text or ""
        buttons = self.driver.find_elements(By.CSS_SELECTOR, self.sel["copy_button"])
        if not buttons:
            return ""
        try:
            text = self.driver.execute_script(
                """
                var el = arguments[0], editorSel = arguments[1], copySel = arguments[2];
                var editor = document.querySelector(editorSel);
                var best = '';
                for (var i = 0; i < 12 && el; i++) {
                  el = el.parentElement;
                  if (!el) break;
                  if (editor && el.contains(editor)) break;              // 入力欄まで含む大きさは行き過ぎ
                  if (el.querySelectorAll(copySel).length > 1) break;    // 他の回答まで含んだら行き過ぎ
                  // ボタン類（コピー・いいね等）を除いた本文
                  var clone = el.cloneNode(true);
                  clone.querySelectorAll('button, [role=toolbar], [role=button]').forEach(function (b) { b.remove(); });
                  var body = (clone.innerText || '').trim();
                  if (body.length > 0) best = body;                      // 広げられる限り広げた入れ物の本文
                }
                return best;
                """, buttons[-1], self.sel["editor"], self.sel["copy_button"])
            return text or ""
        except Exception:  # noqa: BLE001
            return ""

    def send(self, text: str) -> str:
        ok, reason = self._alive()
        if not ok:
            # 切れていたら起動し直し、呼び出し側（server）に会話の作り直しを頼む
            self._restart(reason)
            raise WorkerRestarted(f"{self.id}: ブラウザを再起動しました（{reason}）")
        try:
            return self._send(text)
        except BridgeError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            if _is_disconnect(msg):
                self._restart(msg)
                raise WorkerRestarted(f"{self.id}: 操作中に接続が切れたためブラウザを再起動しました（{msg}）") from e
            raise BridgeError(f"{self.id}: 操作に失敗: {msg} url={_safe_url(self.driver)}") from e

    def _send(self, text: str) -> str:
        from selenium.webdriver.common.keys import Keys

        self._focus_chat_window()
        self._log(f"入力欄を探す url={_safe_url(self.driver)}")
        editor = self._editor(30.0)
        before = self._count(self.sel["copy_button"])
        self._log(f"入力 {len(text)} 文字")
        mode = self._type(editor, text)
        time.sleep(0.3)
        # 再描画で参照が古くなることがあるので、送信前に入力欄を取り直す
        try:
            editor = self._find_editor_here() or editor
        except Exception:  # noqa: BLE001
            pass
        editor.send_keys(Keys.ENTER)
        self._log(f"送信（入力方式 {mode}）。回答を待つ")
        t0 = time.monotonic()
        self._wait_done(before, self.cfg.reply_timeout_sec)
        time.sleep(1.0)
        reply = (self._read_last_reply() or "").strip()
        self._log(f"回答 {len(reply)} 文字（{time.monotonic() - t0:.0f} 秒）")
        self.turns += 1
        self.chat_turns += 1
        if not reply:
            raise ModelStalled(f"{self.id}: 回答を読み取れませんでした（コピーボタン/DOM とも空）")
        for phrase in self.cfg.error_phrases:
            if phrase and phrase in reply:
                self.error = phrase
                raise ModelStalled(f"{self.id}: Copilot が制限・障害を返しました（{phrase}）")
        self.error = ""
        return reply


_DISCONNECT_MARKS = ("not connected to devtools", "disconnected", "invalid session id",
                     "no such window", "target window already closed", "chrome not reachable",
                     "session deleted", "target frame detached")


def _is_disconnect(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in _DISCONNECT_MARKS)


def _safe_url(driver) -> str:
    try:
        return driver.current_url if driver is not None else "-"
    except Exception:  # noqa: BLE001
        return "?"


# ---------------------------------------------------------------- クリップボード

def _clipboard_write(value: str) -> bool:
    try:
        import pyperclip

        pyperclip.copy(value)
        return True
    except Exception:  # noqa: BLE001
        return False


def _clipboard_read(driver=None) -> str:
    try:
        import pyperclip

        v = pyperclip.paste()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    # navigator.clipboard.readText() は使わない: 自動操作中は権限待ちで戻らないことがある
    # （模擬ページで再現。script timeout も効かず固まった）。
    # tkinter も使わない: 作業担当はスレッドで動くため、メインスレッド以外からの Tk() は固まる。
    # pyperclip が無ければ空を返し、呼び出し側が DOM から本文を読む
    return ""
