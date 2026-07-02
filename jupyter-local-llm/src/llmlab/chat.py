"""会話ヘルパーと JupyterLab 用マジックコマンド。

ノートブックのセルで以下のように使える::

    %load_ext llmlab.chat        # 拡張をロード
    %%llm                        # セル本文をプロンプトとして送信（応答をストリーム表示）
    このDataFrameを集計するコードを書いて

会話履歴を保持したい場合は `Chat` クラスを直接使う::

    from llmlab.chat import Chat
    chat = Chat(system="あなたは丁寧なPythonアシスタントです")
    print(chat.ask("CSVを読み込むコードは？"))
"""

from __future__ import annotations

from .client import get_client
from .config import get_settings

DEFAULT_SYSTEM = (
    "あなたは熟練のソフトウェアエンジニアです。"
    "簡潔に、実行可能なコードと短い説明で回答してください。"
)


class Chat:
    """会話履歴を保持するチャットセッション。"""

    def __init__(self, system: str | None = DEFAULT_SYSTEM, **default_kwargs):
        get_settings()  # 未設定なら早期に分かりやすいエラーを出す（接続情報は都度解決する）
        self._default_kwargs = default_kwargs
        self.history: list[dict[str, str]] = []
        if system:
            self.history.append({"role": "system", "content": system})

    def ask(self, prompt: str, *, stream: bool = False, **kwargs) -> str:
        """プロンプトを送信し、応答を履歴に追加して返す。"""
        # 設定・クライアントは毎回解決する。構築時に固定すると configure() し直しても
        # 既存の Chat / %%llm が旧接続先を使い続けてしまうため。
        settings = get_settings()
        client = get_client()
        self.history.append({"role": "user", "content": prompt})
        call_kwargs = {**self._default_kwargs, **kwargs}

        try:
            if stream:
                chunks: list[str] = []
                response = client.chat.completions.create(
                    model=settings.model,
                    messages=self.history,
                    stream=True,
                    **call_kwargs,
                )
                for event in response:
                    if not event.choices:  # 一部サーバは choices が空のチャンクを送る
                        continue
                    delta = event.choices[0].delta.content or ""
                    if delta:
                        chunks.append(delta)
                        print(delta, end="", flush=True)
                print()
                answer = "".join(chunks)
            else:
                resp = client.chat.completions.create(
                    model=settings.model,
                    messages=self.history,
                    **call_kwargs,
                )
                answer = (resp.choices[0].message.content or "") if resp.choices else ""
        except Exception:
            self.history.pop()  # 失敗した user メッセージを履歴に残さない（以後の会話の汚染防止）
            raise

        self.history.append({"role": "assistant", "content": answer})
        return answer

    def reset(self) -> None:
        """先頭の system メッセージだけ残して履歴を消す。"""
        self.history = self.history[:1] if self.history and self.history[0]["role"] == "system" else []


def load_ipython_extension(ipython) -> None:
    """`%load_ext llmlab.chat` で呼ばれる登録フック。"""
    from IPython.core.magic import Magics, line_cell_magic, magics_class

    @magics_class
    class _LLMMagics(Magics):
        def __init__(self, shell):
            super().__init__(shell)
            self._chat: Chat | None = None  # 初回利用時に生成（設定後でよい）

        def _ensure_chat(self) -> Chat:
            if self._chat is None:
                self._chat = Chat()  # 未設定ならここで分かりやすい例外
            return self._chat

        @line_cell_magic
        def llm(self, line: str, cell: str | None = None):
            """`%llm 質問`（1行）または `%%llm`（セル本文）でLLMに送信する。

            `%%llm --new` でセッション履歴をリセットしてから送信。
            """
            reset = line.strip() == "--new"
            prompt = (cell if cell is not None else line).strip()
            if reset:
                if self._chat is not None:
                    self._chat.reset()
                if cell is None:
                    return
                prompt = cell.strip()
            if not prompt:
                print("（プロンプトが空です）")
                return
            self._ensure_chat().ask(prompt, stream=True)

    ipython.register_magics(_LLMMagics)


def chat_panel(system: str | None = DEFAULT_SYSTEM):
    """ノートブック内のチャット UI（ipywidgets）を表示する（jupyter-ai 不要）。"""
    from .config import widget_env

    ok, reason = widget_env()
    if not ok:
        print(f"chat_panel はブラウザのノートブック上の ipywidgets が必要です（{reason}）。")
        print("代わりに次を使ってください: %load_ext llmlab.chat → %%llm、"
              "もしくは llmlab.complete('質問')。診断は llmlab.doctor()。")
        return

    import ipywidgets as widgets
    from IPython.display import display

    chat = Chat(system=system)
    log = widgets.HTML(
        value="", layout=widgets.Layout(width="100%", height="320px",
                                         overflow_y="auto", border="1px solid #ddd", padding="8px")
    )
    box = widgets.Text(placeholder="メッセージを入力して Enter", layout=widgets.Layout(width="80%"))
    send = widgets.Button(description="送信", button_style="primary")
    clear = widgets.Button(description="クリア")
    history_html: list[str] = []

    def _render():
        log.value = "".join(history_html)

    def _escape(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    def _submit(_=None):
        text = box.value.strip()
        if not text:
            return
        box.value = ""
        history_html.append(f"<p><b>🧑 You:</b><br>{_escape(text)}</p>")
        _render()
        try:
            answer = chat.ask(text)  # 非ストリームで取得して一括表示
        except Exception as e:  # noqa: BLE001
            answer = f"⚠️ エラー: {e}"
        history_html.append(f"<p><b>🤖 LLM:</b><br>{_escape(answer)}</p><hr>")
        _render()

    def _clear(_):
        chat.reset()
        history_html.clear()
        _render()

    send.on_click(_submit)
    clear.on_click(_clear)
    # Enter キー送信。ipywidgets 8 では Text.on_submit が削除されたため安全にガードする
    if hasattr(box, "on_submit"):
        try:
            box.on_submit(_submit)
        except Exception:  # noqa: BLE001
            pass
    display(widgets.VBox([log, widgets.HBox([box, send, clear])]))
