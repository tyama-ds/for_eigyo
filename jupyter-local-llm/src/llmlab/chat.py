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
        self._settings = get_settings()
        self._client = get_client()
        self._default_kwargs = default_kwargs
        self.history: list[dict[str, str]] = []
        if system:
            self.history.append({"role": "system", "content": system})

    def ask(self, prompt: str, *, stream: bool = False, **kwargs) -> str:
        """プロンプトを送信し、応答を履歴に追加して返す。"""
        self.history.append({"role": "user", "content": prompt})
        call_kwargs = {**self._default_kwargs, **kwargs}

        if stream:
            chunks: list[str] = []
            response = self._client.chat.completions.create(
                model=self._settings.model,
                messages=self.history,
                stream=True,
                **call_kwargs,
            )
            for event in response:
                delta = event.choices[0].delta.content or ""
                if delta:
                    chunks.append(delta)
                    print(delta, end="", flush=True)
            print()
            answer = "".join(chunks)
        else:
            resp = self._client.chat.completions.create(
                model=self._settings.model,
                messages=self.history,
                **call_kwargs,
            )
            answer = resp.choices[0].message.content or ""

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
