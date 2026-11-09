"""ResearcherSession（M365 Copilot リサーチツールの半自動操作）のテスト。

実ブラウザは使わず、Edge の振る舞いを模した偽ドライバで次を確かめる:
- 自動クリックが「別のエージェント」を開いても、人の確認（assist）で正しく続行できる
- 自動クリック自体が失敗しても、人に選んでもらって続行できる
- 人が別タブでリサーチツールを開いた場合、そのタブへ切り替える
- 同じチャットで章を続けても、各章で「新しく出た」長さボタン/コピーボタンだけを押す
- 2 章目以降に長さの選択肢が出ない場合の go ahead の要否
- 人が「中止」したらエラーにする
"""

from __future__ import annotations

import pytest

pytest.importorskip("selenium")

from selenium.common.exceptions import NoSuchElementException  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.remote.webelement import WebElement  # noqa: E402

from llmlab import m365_user_script as us  # noqa: E402


class _El(WebElement):
    # EC.element_to_be_clickable は WebElement のインスタンスだけを要素として扱う
    def __init__(self, drv, kind, idx=0):  # WebElement.__init__ は呼ばない（偽要素）
        self.drv, self.kind, self.idx = drv, kind, idx

    def click(self):
        self.drv.on_click(self)

    def send_keys(self, *keys):
        self.drv.on_keys(keys)

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True


class _SwitchTo:
    def __init__(self, drv):
        self.drv = drv

    def window(self, handle):
        self.drv.current_window_handle = handle


class FakeEdge:
    """M365 Copilot の画面を最小限に模した偽 Edge。

    entrance_opens: エージェント入口（原文の XPath）を押したときに開く画面
                    "researcher" / "other"（別エージェント）/ None（入口が見つからない）
    later:          2 章目以降の送信に対するリサーチツールの反応
                    "length"（長さの選択肢を出す）/ "direct"（すぐ調査開始）/
                    "clarify"（確認の質問だけして待つ）
    """

    def __init__(self, entrance_opens="researcher", later="length"):
        self.entrance_opens = entrance_opens
        self.later = later
        self.pages = {"w1": "blank"}
        self.current_window_handle = "w1"
        self.switch_to = _SwitchTo(self)
        self.sent_prompts: list[str] = []
        self.go_aheads = 0
        self.length_buttons = 0
        self.length_clicked: list[int] = []
        self.responses: list[str] = []
        self.generating = 0
        self.clip = ""
        self.quit_called = False

    # ---- 画面状態 ----
    @property
    def page(self):
        return self.pages[self.current_window_handle]

    @page.setter
    def page(self, value):
        self.pages[self.current_window_handle] = value

    @property
    def window_handles(self):
        return list(self.pages)

    def human_opens_researcher(self, new_tab=False):
        if new_tab:
            self.pages["w2"] = "researcher"
        else:
            self.page = "researcher"

    # ---- WebDriver API ----
    def get(self, url):
        self.page = "home"

    def quit(self):
        self.quit_called = True

    def refresh(self):
        pass

    def execute_script(self, script, *args):
        return "complete" if "readyState" in script else None

    def execute_async_script(self, script, *args):
        return self.clip if "readText" in script else None

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException(value)
        return found[0]

    def find_elements(self, by, value):
        page = self.page
        if value == us.MENU_XPATH:
            return [_El(self, "menu")] if page == "home" else []
        if value == us.AGENT_ENTRANCE_XPATH:
            ok = page == "home_menu" and self.entrance_opens is not None
            return [_El(self, "entrance")] if ok else []
        if (by, value) == us.CLOSE_TAB_LOCATOR:
            return [_El(self, "close")] if page in ("researcher", "other") else []
        if value == us.EDITOR_ID:
            return [_El(self, "editor")] if page in ("researcher", "other") else []
        if (by, value) == us.LENGTH_LOCATOR:
            return [_El(self, "length", i) for i in range(self.length_buttons)]
        if (by, value) == us.COPY_BUTTON:
            return [_El(self, "copy", i) for i in range(len(self.responses))]
        if (by, value) == us.STOP_BUTTON:
            if self.generating > 0:
                self.generating -= 1
                if self.generating == 0:  # 生成完了 → 応答とコピーボタンが増える
                    self.responses.append(f"回答{len(self.responses) + 1}（{self.page}）")
                return [_El(self, "stop")]
            return []
        return []

    # ---- 操作への反応 ----
    def on_click(self, el):
        if el.kind == "menu":
            self.page = "home_menu"
        elif el.kind == "entrance":
            self.page = self.entrance_opens
        elif el.kind == "length":
            self.length_clicked.append(el.idx)
        elif el.kind == "copy":
            self.clip = self.responses[el.idx]

    def on_keys(self, keys):
        text = "".join(k for k in keys if isinstance(k, str) and k not in (Keys.ENTER,))
        if Keys.ENTER not in keys:
            return
        if text == "go ahead":
            self.go_aheads += 1
            self.generating = 3
            return
        self.sent_prompts.append(text)
        first = len(self.sent_prompts) == 1
        if self.page != "researcher" or (not first and self.later == "direct"):
            self.generating = 3  # 普通のチャット / すぐ調査を始めるケース
        elif first or self.later == "length":
            self.length_buttons += 1  # 長さの選択肢を出して待つ
        # later == "clarify": 何も出さずに待つ（確認の質問だけ）


@pytest.fixture
def fast(monkeypatch):
    """待ち時間（sleep）を潰して高速化する。"""
    real_sleep = us.time.sleep
    monkeypatch.setattr(us.time, "sleep", lambda s: real_sleep(min(s, 0.001)))


def _session(drv, monkeypatch, *, agent_select="assist", human=None, length_timeout=0.5):
    monkeypatch.setattr(us.webdriver, "Edge", lambda service=None, **kw: drv)
    asked: list[str] = []

    def ask_human(message):
        asked.append(message)
        return human(drv, len(asked)) if human else True

    s = us.ResearcherSession(driver_path="dummy", agent_select=agent_select,
                             ask_human=ask_human, length_timeout_sec=length_timeout)
    s.editor_timeout_sec = 0.2
    return s, asked


def _fix_agent(drv, n):
    drv.human_opens_researcher()
    return True


def test_assist_recovers_when_auto_click_opens_wrong_agent(monkeypatch, fast):
    drv = FakeEdge(entrance_opens="other")
    s, asked = _session(drv, monkeypatch, human=_fix_agent)
    text = s.research("第1章のプロンプト")
    assert len(asked) == 1                      # 人への確認は 1 回だけ
    assert drv.page == "researcher"             # 人が選び直した
    assert text == "回答1（researcher）"
    assert drv.length_clicked == [0] and drv.go_aheads == 1


def test_assist_recovers_when_auto_click_fails(monkeypatch, fast):
    drv = FakeEdge(entrance_opens=None)          # 入口が見つからない
    s, asked = _session(drv, monkeypatch, human=_fix_agent)
    assert s.research("p") == "回答1（researcher）"
    assert len(asked) == 1


def test_auto_mode_raises_without_asking(monkeypatch, fast):
    drv = FakeEdge(entrance_opens=None)
    s, asked = _session(drv, monkeypatch, agent_select="auto", human=_fix_agent)
    with pytest.raises(NoSuchElementException):
        s.research("p")
    assert asked == []


def test_manual_mode_skips_auto_clicks(monkeypatch, fast):
    drv = FakeEdge(entrance_opens="other")
    s, asked = _session(drv, monkeypatch, agent_select="manual", human=_fix_agent)
    assert s.research("p") == "回答1（researcher）"
    assert drv.pages["w1"] == "researcher" and len(asked) == 1


def test_switches_to_new_tab_opened_by_human(monkeypatch, fast):
    drv = FakeEdge(entrance_opens="other")

    def open_in_new_tab(d, n):
        d.pages["w1"] = "home"                   # 元のタブは入力欄なし
        d.human_opens_researcher(new_tab=True)
        return True

    s, _ = _session(drv, monkeypatch, human=open_in_new_tab)
    assert s.research("p") == "回答1（researcher）"
    assert drv.current_window_handle == "w2"


def test_retries_until_editor_found_then_aborts(monkeypatch, fast):
    drv = FakeEdge(entrance_opens=None)
    s, asked = _session(drv, monkeypatch, human=lambda d, n: True)   # 押すだけで何もしない
    with pytest.raises(RuntimeError, match="見つかりませんでした"):
        s.research("p")
    assert len(asked) == 3


def test_abort_by_human(monkeypatch, fast):
    drv = FakeEdge(entrance_opens="other")
    s, _ = _session(drv, monkeypatch, human=lambda d, n: False)
    with pytest.raises(RuntimeError, match="中止"):
        s.research("p")


@pytest.mark.parametrize("later, go_aheads", [("length", 2), ("clarify", 2), ("direct", 1)])
def test_same_chat_chapters_use_newest_buttons(monkeypatch, fast, later, go_aheads):
    drv = FakeEdge(entrance_opens="researcher", later=later)
    s, asked = _session(drv, monkeypatch, human=lambda d, n: True)
    first = s.research("第1章")
    second = s.research("第2章")
    assert (first, second) == ("回答1（researcher）", "回答2（researcher）")
    assert len(asked) == 1                      # 2 章目は聞かない
    assert drv.sent_prompts == ["第1章", "第2章"]
    assert drv.go_aheads == go_aheads
    expected_clicks = [0, 1] if later == "length" else [0]
    assert drv.length_clicked == expected_clicks  # 古い長さボタンは押さない


def test_close_quits_browser(monkeypatch, fast):
    drv = FakeEdge()
    s, _ = _session(drv, monkeypatch)
    s.research("p")
    s.close()
    assert drv.quit_called and s.driver is None
