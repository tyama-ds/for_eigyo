"""ユーザー提供スクリプト（M365 Copilot リサーチツール操作）を収めたモジュール。

このファイルの Selenium 操作は、実運用で動作確認済みのスクラッチスクリプトの原文です。
テナント / 言語 / UI 改定でセレクタや XPath が変わったときは、下の「セレクタ」定数を
直接編集してください（原文と同じ値。``run_research`` と ``ResearcherSession`` の両方が使う）。

原文からの変更:
- RESEARCH_PROMPT とドライバのパスを引数化（``run_research()``）
- 応答コピー後にクリップボードを読んで返す（アプリへ本文を渡すため）
- セレクタを定数にまとめた（値は原文のまま）
- 本フローで未使用の import（numpy / pandas / requests / BeautifulSoup / PIL / tqdm 等）と
  proxies 定数を省略
- アプリ用に ``ResearcherSession`` を追加（下記）

``ResearcherSession`` — リサーチツールの選択を人に任せられる、章を続けて調べるセッション:
  原文の絶対 XPath（「エージェント一覧の 1 番目」）は UI の並びが変わると別のエージェントを
  開いてしまう。そこで「リサーチツールを選ぶ」1 ステップだけを人に確認/代行してもらい、
  残り（送信→長さ選択→go ahead→完了待ち→コピー）は自動で行う。同じチャットで章を続けて
  調べるので、人の操作は 1 回の実行につき最初の 1 回だけ。
"""

from __future__ import annotations

import time
from collections.abc import Callable

##################################################################
#####################modules and constants########################
##################################################################
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

###value###
DRIVER_PATH = r"C:\Driver\edgedriver_win32\msedgedriver.exe"
LOGIN_URL = 'https://www.office.com/?auth=2'

###セレクタ（原文と同じ値。UI が変わったらここを直す）###
MENU_XPATH = '/html/body/div[1]/div/div/main/div[1]/div/header/div/div[1]/button'
AGENT_ENTRANCE_XPATH = ('/html/body/div[1]/div/div/main/div[1]/div/div/div[2]/div/div[1]'
                        '/div[2]/div[1]/div/button[1]/span[2]')
CLOSE_TAB_LOCATOR = (By.CSS_SELECTOR, "button[data-testid='header-branding-container']")
EDITOR_ID = "m365-chat-editor-target-element"
LENGTH_LOCATOR = (By.CSS_SELECTOR, "button[aria-label='長い, 5 ページ以上']")
COPY_BUTTON = (By.CSS_SELECTOR, "button[aria-label='応答のコピー']")

#########################################################
#####################Set_Function########################
#########################################################

#M365へログイン
def _login():
    login_url=LOGIN_URL
    #options = webdriver.ChromeOptions()
    driver =  webdriver.Edge(service=Service(DRIVER_PATH))
    # Googleの検索TOP画面を開く。
    driver.get(login_url)
    # 3秒待機
    time.sleep(1)
    return driver


#生成が終わるまで待つ。
STOP_BUTTON = (By.CSS_SELECTOR, "button[aria-label='生成を停止する']")
def wait_until_generation_done(driver, poll_sec=1, refresh_every_sec=600, total_timeout_sec=None):
    """
    生成中(■)→ 完了(⇒)に戻るまで待つ。
    1秒おきに確認。10分見つからない/止まらないなら refresh。

    Returns
    -------
    True  : 完了を検知
    """
    start = time.monotonic()
    last_refresh = start
    saw_stop = False  # 一度でも■を見たか

    while True:
        now = time.monotonic()

        if total_timeout_sec is not None and (now - start) > total_timeout_sec:
            raise TimeoutError("生成完了を検出できませんでした。")

        try:
            stops = driver.find_elements(*STOP_BUTTON)
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


def _read_clipboard(driver) -> str:
    """クリップボード本文を取得（pyperclip → JS → tkinter の順に試す）。※アプリ用の追加"""
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
            v = r.clipboard_get()
        finally:
            r.destroy()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return ""


def _open_researcher_auto(driver):
    """原文の「メニュー → リサーチツール → タブを閉じる → 10 秒待機」。"""
    driver.find_element(By.XPATH, MENU_XPATH).click()
    _agent_entrance=driver.find_element(By.XPATH, AGENT_ENTRANCE_XPATH)

    #リサーチツールをクリックして移動。
    _agent_entrance.click()

    LOCATOR = CLOSE_TAB_LOCATOR
    btn = WebDriverWait(driver, 60).until(EC.element_to_be_clickable(LOCATOR))#タブを閉じる
    btn.click()

    time.sleep(10)


def run_research(research_prompt, driver_path=None, total_timeout_sec=None):
    """スクリプト本体（原文の実行部）を 1 回実行し、コピーされた本文を返す。

    失敗（例外）時はブラウザを **開いたまま** にする（原文と同じ。どの画面で
    止まったか目視で確認できる）。
    """
    global DRIVER_PATH
    if driver_path:
        DRIVER_PATH = driver_path

    RESEARCH_PROMPT = research_prompt

    #ログインして
    driver=_login()
    _open_researcher_auto(driver)
    driver.find_element(By.ID, EDITOR_ID).send_keys(RESEARCH_PROMPT, Keys.ENTER)
    #active = test.switch_to.active_element
    #active.send_keys("テスト文字") #レポートの長い方を選ぶ。下へ移動してクリック。
    elem = WebDriverWait(driver, 50).until(
        EC.element_to_be_clickable(LENGTH_LOCATOR)
    )
    driver.execute_script("arguments[0].scrollIntoView();", elem)
    time.sleep(5)
    elem = WebDriverWait(driver, 50).until(
        # よくわからないけど、ここで再定義必要みたい。
        EC.element_to_be_clickable(LENGTH_LOCATOR)
    )
    elem.click()#
    elem = driver.find_element(By.ID, EDITOR_ID)

    elem.click()

    # 全選択 → 削除
    elem.send_keys(Keys.CONTROL, "a")
    elem.send_keys(Keys.BACKSPACE)

    elem.send_keys("go ahead", Keys.ENTER)##これだとおまかせ
    """# 入力＆送信
    elem = driver.find_element(By.ID, "m365-chat-editor-target-element")
    elem.click()
    elem.send_keys("テスト")
    elem.send_keys(Keys.ENTER)
    """
    # 生成完了まで待つ
    wait_until_generation_done(driver, total_timeout_sec=total_timeout_sec)

    print("✅ 生成完了！")

    # ① 出現＆クリック可能まで待つ
    copy_btn = WebDriverWait(driver, 60).until(
        EC.element_to_be_clickable(COPY_BUTTON)
    )

    # ② クリック
    copy_btn.click()

    time.sleep(1)                       # ※追加: コピー反映待ち
    text = _read_clipboard(driver)      # ※追加: 本文をアプリへ返す

    driver.quit()
    return text


#########################################################
#####################アプリ用セッション####################
#########################################################

AskHumanFn = Callable[[str], bool]   # 案内文を見せ「準備完了」なら True、中止なら False
LogFn = Callable[[str], None]

_SELECT_MESSAGE = (
    "Edge でリサーチツールを開いてください。\n"
    "① サインインを求められたら完了する\n"
    "② エージェント一覧から「リサーチツール」を選ぶ"
    "（自動で開いていれば、リサーチツールになっているか確認するだけで OK）\n"
    "③ チャットの入力欄が表示されたら「準備完了」を押す"
)
_RETRY_MESSAGE = (
    "リサーチツールの入力欄が見つかりませんでした。\n"
    "Edge の画面がリサーチツールのチャット（入力欄がある画面）になっているか確認して、"
    "もう一度「準備完了」を押してください。"
)


class ResearcherSession:
    """同じ Edge・同じリサーチツールのチャットで、章を続けて調べるセッション。

    agent_select（リサーチツールの選び方）:
      "assist" : 原文どおり自動クリックを試した後、人に確認してもらう（既定）。
                 自動で正しく開けていれば「準備完了」を押すだけ。違えば人が選び直す。
      "manual" : 自動クリックはせず、人がリサーチツールを選ぶ。
      "auto"   : 原文どおり自動クリックのみ（人に聞かない）。
    researcher_url を指定すると、メニューを辿らずにその URL を直接開く（リサーチツールを
    開いた状態のアドレスバーの URL をコピーして使う）。

    人への確認は 1 セッション（＝1 回の実行）につき最初の 1 回だけ。2 章目以降は同じ
    チャットに続けて送信するので、リサーチツールが前の章の文脈を引き継ぐ。
    """

    def __init__(self, driver_path: str | None = None, total_timeout_sec: float | None = None,
                 agent_select: str = "assist", researcher_url: str = "",
                 ask_human: AskHumanFn | None = None, log: LogFn | None = None,
                 length_timeout_sec: float = 50):
        self.driver_path = driver_path
        self.total_timeout_sec = total_timeout_sec
        self.agent_select = (agent_select or "assist").lower()
        self.researcher_url = (researcher_url or "").strip()
        self.ask_human = ask_human
        self.log = log or (lambda msg: None)
        self.length_timeout_sec = float(length_timeout_sec)
        self.editor_timeout_sec = 15.0   # 「準備完了」後に入力欄を探す秒数
        self.driver = None

    # ---- 開く（ログイン → リサーチツール） ---------------------------------------------

    def open(self) -> None:
        global DRIVER_PATH
        if self.driver_path:
            DRIVER_PATH = self.driver_path
        self.log("Edge を起動して office.com を開きます")
        self.driver = _login()
        if self.researcher_url:
            self.log("指定の URL でリサーチツールを開きます")
            self.driver.get(self.researcher_url)
            time.sleep(5)
        elif self.agent_select in ("assist", "auto"):
            try:
                self.log("リサーチツールを自動で開きます（原文の手順）")
                _open_researcher_auto(self.driver)
            except Exception as e:  # noqa: BLE001
                if self.agent_select == "auto":
                    raise
                self.log(f"自動で開けませんでした（{type(e).__name__}）。手動で選んでください")
        if self.agent_select in ("assist", "manual"):
            self._confirm_by_human()
        elif not self._find_editor(30):
            raise RuntimeError("リサーチツールの入力欄が見つかりません"
                               "（agent_select を 'assist' にすると人が選び直せます）")

    def _confirm_by_human(self) -> None:
        if self.ask_human is None:
            raise RuntimeError("リサーチツールの選択を人に確認する手段がありません"
                               "（アプリの画面から実行するか、agent_select='auto' にしてください）")
        message = _SELECT_MESSAGE
        for _ in range(3):
            self.log("リサーチツールの選択を待っています（アプリ画面の「準備完了」を押してください）")
            if not self.ask_human(message):
                raise RuntimeError("リサーチツールの選択が中止されました")
            if self._find_editor(self.editor_timeout_sec):
                self.log("リサーチツールの入力欄を確認しました")
                return
            message = _RETRY_MESSAGE
        raise RuntimeError("リサーチツールの入力欄が見つかりませんでした（3 回確認しました）")

    def _find_editor(self, timeout: float) -> bool:
        """入力欄があるか確認する。人が別タブで開いた場合はそのタブへ切り替える。"""
        driver = self.driver
        deadline = time.monotonic() + timeout
        while True:
            if driver.find_elements(By.ID, EDITOR_ID):
                return True
            current = driver.current_window_handle
            for handle in driver.window_handles:
                if handle == current:
                    continue
                driver.switch_to.window(handle)
                if driver.find_elements(By.ID, EDITOR_ID):
                    return True
            driver.switch_to.window(current)
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)

    # ---- 1 章分（原文の手順。同じチャットで続けられるよう「新しく出たボタン」を押す） ----

    def research(self, prompt: str) -> str:
        if self.driver is None:
            self.open()
        driver = self.driver
        # 同じチャットでは前の章のボタンも残っているので、送信前の個数を控えておく
        n_copy_before = len(driver.find_elements(*COPY_BUTTON))
        n_length_before = len(driver.find_elements(*LENGTH_LOCATOR))

        self.log("プロンプトを送信します")
        driver.find_element(By.ID, EDITOR_ID).send_keys(prompt, Keys.ENTER)

        #レポートの長い方を選ぶ。下へ移動してクリック。（新しく出たボタンだけを対象にする）
        clicked_length = False
        try:
            WebDriverWait(driver, self.length_timeout_sec).until(
                lambda d: len(d.find_elements(*LENGTH_LOCATOR)) > n_length_before)
            elem = driver.find_elements(*LENGTH_LOCATOR)[-1]
            driver.execute_script("arguments[0].scrollIntoView();", elem)
            time.sleep(5)
            # よくわからないけど、ここで再定義必要みたい。
            elem = driver.find_elements(*LENGTH_LOCATOR)[-1]
            WebDriverWait(driver, 50).until(EC.element_to_be_clickable(elem)).click()
            clicked_length = True
            self.log("レポートの長さ「長い」を選びました")
        except TimeoutException:
            self.log("長さの選択肢は出ませんでした")

        # 長さを選んだ、または（選択肢が出ず）生成も始まっていない＝確認待ち → おまかせで続行
        if clicked_length or not _is_generating(driver):
            elem = driver.find_element(By.ID, EDITOR_ID)
            elem.click()
            # 全選択 → 削除
            elem.send_keys(Keys.CONTROL, "a")
            elem.send_keys(Keys.BACKSPACE)
            elem.send_keys("go ahead", Keys.ENTER)##これだとおまかせ

        # 生成完了まで待つ
        self.log("リサーチの完了を待っています（■ が消えるまで）")
        wait_until_generation_done(driver, total_timeout_sec=self.total_timeout_sec)

        # ① 今回の応答のコピーボタンが出るまで待つ ② いちばん新しいものをクリック
        WebDriverWait(driver, 60).until(
            lambda d: len(d.find_elements(*COPY_BUTTON)) > n_copy_before)
        driver.find_elements(*COPY_BUTTON)[-1].click()
        time.sleep(1)                       # コピー反映待ち
        self.log("応答をコピーしました")
        return _read_clipboard(driver)

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self.driver = None


def _is_generating(driver) -> bool:
    try:
        return any(e.is_displayed() for e in driver.find_elements(*STOP_BUTTON))
    except StaleElementReferenceException:
        return True
