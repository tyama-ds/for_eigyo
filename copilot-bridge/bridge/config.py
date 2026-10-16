"""設定。優先順位は 引数 > 環境変数（COPILOT_BRIDGE_*） > config.json > 既定値。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

ENV_PREFIX = "COPILOT_BRIDGE_"

# Copilot 画面の操作に使うセレクタ。UI 改定で変わったら config.json の "selectors" で上書きする。
DEFAULT_SELECTORS = {
    # 入力欄（実運用で確認済み）
    "editor": "#m365-chat-editor-target-element",
    # 生成中に表示される停止ボタン（消えたら完了）
    "stop_button": "button[aria-label='生成を停止する']",
    # 回答のコピーボタン（最後のものが最新の回答）
    "copy_button": "button[aria-label='応答のコピー']",
    # コピーが使えないときに本文を直接読む要素（未確認。実機で確認して直す）
    "response": "[data-testid='chat-message-content'], [data-tid='chat-message-content']",
    # 会話の削除（先頭が // なら XPath、それ以外は CSS）。実機の DOM: 会話一覧の 1 行は Fluent の
    # SplitNavItem（class に SplitNavItem を含む div）、削除は role="menuitem" の div（class に MenuItem）
    # 今開いている会話の行（選択状態が付いたもの。無ければ先頭 = 最新）
    "chat_item": "//*[contains(@class,'SplitNavItem')][.//*[@aria-current] or @aria-current or @aria-selected='true' or contains(@class,'selected')] | (//*[contains(@class,'SplitNavItem')])[1]",
    # 行の中の「…」ボタン（行に対する相対 XPath。ホバーしてから押す）
    "chat_menu_button": ".//button[last()]",
    "delete_menu_item": "//*[@role='menuitem'][contains(., '削除') or contains(., 'Delete')]",
    # 確認ダイアログのボタン（出なければ省略）
    "confirm_delete": "//*[@role='dialog' or @role='alertdialog']//button[contains(., '削除') or contains(., 'Delete')]",
    # 会話一覧が畳まれているときに開くボタン（任意）
    "history_button": "//button[contains(@aria-label,'ナビゲーション') or contains(@aria-label,'履歴') or contains(@aria-label,'チャット')]",
}

# 回答にこれらが含まれていたら、その担当は制限や障害に当たったとみなす
DEFAULT_ERROR_PHRASES = [
    "上限に達しました",
    "しばらくしてからもう一度",
    "問題が発生しました",
    "You've reached the limit",
    "Something went wrong",
]


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8800
    model_name: str = "copilot"
    # edge: Selenium で Edge を操作 / fake: ブラウザ無し（テスト・開発用）
    backend: str = "edge"
    # 同時に開くウインドウ数（＝同時に進められる会話数）
    windows: int = 2
    driver_path: str = r"C:\Driver\edgedriver_win32\msedgedriver.exe"
    # ウインドウごとの Edge プロファイル置き場（空なら %LOCALAPPDATA%\copilot-bridge\profiles）
    profile_dir: str = ""
    # Edge の起動方式。auto: separate（専用プロファイル）→ temp（プロファイル指定なし）→ bare（素の起動）
    # → manager（Selenium Manager が Edge の版に合う msedgedriver を取得）の順に試す
    profile_mode: str = "auto"
    # 文字入力の方式。auto: js（execCommand）→ paste（Ctrl+V）→ keys（1 行ずつ）。cdp は DevTools プロトコル
    # （社内ポリシーで接続切れを招くことがあるため既定では使わない）
    input_mode: str = "auto"
    chat_url: str = "https://m365.cloud.microsoft/chat/?auth=2"
    # 起動後、入力欄が出るまで待つ上限（SSO が通らず手でログインする時間を含む）
    login_timeout_sec: float = 300.0
    # 1 回の回答を待つ上限
    reply_timeout_sec: float = 600.0
    # 空きウインドウを待つ上限
    acquire_timeout_sec: float = 900.0
    # ウインドウを並べる位置（index 順に横へずらす）
    window_width: int = 900
    window_height: int = 900
    # Copilot へ送るツール結果 1 件の上限文字数（超えたら中央を省略）
    max_tool_result_chars: int = 12000
    # 会話を作り直して全履歴を送るときの上限文字数
    max_resend_chars: int = 40000
    # ツールがあるのに <tool_call> を出さず「自分で探して無かった」等と答えたときの言い直し回数
    nudge_retries: int = 2
    # 送受信ログの保存先（相対なら copilot-bridge フォルダ基準。空なら保存しない）。
    # <log_dir>/YYYYMMDD.jsonl に全往復、<log_dir>/sessions/<日時>_<会話>.md に会話ごとの全文を残す
    log_dir: str = "logs"
    # 使い終えた Copilot の会話を画面操作で削除する（次の会話に移るとき・終了時）。
    # 削除メニューのセレクタ（selectors.chat_menu_button / delete_menu_item / confirm_delete）は要確認なので既定は無効
    delete_chat_on_close: bool = False
    selectors: dict = field(default_factory=lambda: dict(DEFAULT_SELECTORS))
    error_phrases: list = field(default_factory=lambda: list(DEFAULT_ERROR_PHRASES))

    def resolved_profile_dir(self) -> Path:
        if self.profile_dir:
            return Path(self.profile_dir).expanduser()
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
        return Path(base) / "copilot-bridge" / "profiles"

    def to_dict(self) -> dict:
        return asdict(self)


def _coerce(value: str, kind):
    if kind is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    if kind in (dict, list):
        return json.loads(value)
    return value


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    """config.json → 環境変数 → overrides の順に重ねて Config を作る。"""
    cfg = Config()
    data: dict = {}
    if path and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    for f in fields(Config):
        if f.name in data and data[f.name] is not None:
            v = data[f.name]
            if f.name == "selectors":
                v = {**DEFAULT_SELECTORS, **v}
            setattr(cfg, f.name, v)
        env = os.environ.get(ENV_PREFIX + f.name.upper())
        if env is not None:
            setattr(cfg, f.name, _coerce(env, f.type if isinstance(f.type, type) else type(getattr(cfg, f.name))))
    for k, v in (overrides or {}).items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg
