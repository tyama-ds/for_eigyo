"""ユーザー提供スクリプト（M365 Copilot リサーチツール操作）を **原文のまま** 収めたモジュール。

このファイルの Selenium 操作は、実運用で動作確認済みのスクラッチスクリプトの原文です。
テナント / 言語 / UI 改定でセレクタや XPath が変わったときは、**このファイルを直接編集**
してください（アプリ側の設定は driver_path と total_timeout_sec だけ）。

原文からの変更は次の 3 点のみ:
- RESEARCH_PROMPT とドライバのパスを引数化（``run_research()``）
- 応答コピー後にクリップボードを読んで返す（アプリへ本文を渡すため）
- 本フローで未使用の import（numpy / pandas / requests / BeautifulSoup / PIL / tqdm 等）と
  proxies 定数を省略
"""

from __future__ import annotations

##################################################################
#####################modules and constants########################
##################################################################

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import StaleElementReferenceException

import time

###value###
DRIVER_PATH = r"C:\Driver\edgedriver_win32\msedgedriver.exe"

#########################################################
#####################Set_Function########################
#########################################################

#M365へログイン
def _login():
    login_url='https://www.office.com/?auth=2'
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
    driver.find_element(By.XPATH,'/html/body/div[1]/div/div/main/div[1]/div/header/div/div[1]/button').click()
    _agent_entrance=driver.find_element(By.XPATH,'/html/body/div[1]/div/div/main/div[1]/div/div/div[2]/div/div[1]/div[2]/div[1]/div/button[1]/span[2]')

    #リサーチツールをクリックして移動。
    _agent_entrance.click()

    LOCATOR = (By.CSS_SELECTOR, "button[data-testid='header-branding-container']")
    btn = WebDriverWait(driver, 60).until(EC.element_to_be_clickable(LOCATOR))#タブを閉じる
    btn.click()

    time.sleep(10)
    driver.find_element(By.ID, "m365-chat-editor-target-element").send_keys(RESEARCH_PROMPT,Keys.ENTER)
    #active = test.switch_to.active_element
    #active.send_keys("テスト文字") #レポートの長い方を選ぶ。下へ移動してクリック。
    elem = WebDriverWait(driver, 50).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-label='長い, 5 ページ以上']"))
    )
    driver.execute_script("arguments[0].scrollIntoView();", elem)
    time.sleep(5)
    elem = WebDriverWait(driver, 50).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-label='長い, 5 ページ以上']"))#よくわからないけど、ここで再定義必要みたい。
    )
    elem.click()#
    elem = driver.find_element(By.ID, "m365-chat-editor-target-element")

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


    COPY_BUTTON = (By.CSS_SELECTOR, "button[aria-label='応答のコピー']")

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
