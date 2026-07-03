"""コード補完（jupyter-ai を使わない自前実装）。

OpenAI 互換エンドポイントの chat completions だけを使い、どのサーバでも動くように
している（FIM 専用 API には依存しない）。提供するもの:

- ``code_complete(prefix, suffix="")``  : 関数として補完を取得
- ``%%complete``                        : セルのコードを補完し、続きを次セルへ挿入
- ``completion_panel()``                : 入力欄＋「補完」ボタンの ipywidgets UI

ノート: JupyterLab のセルに直接出る“ゴーストテキスト”はフロントエンド拡張が必要なため、
純 Python では本モジュールのように「セル/パネルで補完を得る」方式で代替する。
"""

from __future__ import annotations

import re

from .client import get_client
from .config import get_settings

_SYSTEM = (
    "You are a code completion engine. Continue the user's code naturally. "
    "Output ONLY the code that should come next — no explanations, no commentary, "
    "and no Markdown code fences."
)
_SYSTEM_FIM = (
    "You are a fill-in-the-middle code completion engine. The user gives code with a "
    "<CURSOR> marker. Output ONLY the code that should replace <CURSOR> so that the "
    "PREFIX and SUFFIX join correctly — no explanations, no Markdown fences."
)

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\n?|\n?```$")


def strip_fences(text: str) -> str:
    """LLM が付けがちな ```lang ... ``` フェンスを除去する。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_+-]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t


def code_complete(
    prefix: str,
    suffix: str = "",
    *,
    language: str = "python",
    max_tokens: int = 512,
    temperature: float = 0.2,
    stop: list[str] | None = None,
) -> str:
    """``prefix`` に続くコードを返す。``suffix`` 指定で fill-in-the-middle になる。"""
    s = get_settings()
    if suffix:
        system = _SYSTEM_FIM
        user = f"Language: {language}\n\n{prefix}<CURSOR>{suffix}"
    else:
        system = _SYSTEM
        user = f"Language: {language}\n\n{prefix}"

    kwargs: dict = {"model": s.model, "temperature": temperature, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
    if stop:
        kwargs["stop"] = stop
    resp = get_client().chat.completions.create(**kwargs)
    from .client import strip_think

    raw = (resp.choices[0].message.content or "") if resp.choices else ""
    return strip_fences(strip_think(raw))  # 思考過程→フェンスの順に除去


def inline_complete(prefix: str, suffix: str = "", *, max_tokens: int = 128) -> str:
    """JupyterLab のインライン補完拡張から呼ばれる入口。

    どんな失敗でも例外を投げず空文字を返す（＝補完なし）。未設定時も空文字。
    インライン用にトークン数を絞って応答を軽くする。
    """
    try:
        from .config import is_configured

        if not is_configured() or not prefix.strip():
            return ""
        return code_complete(prefix, suffix, max_tokens=max_tokens, temperature=0.1)
    except Exception:  # noqa: BLE001
        return ""


def load_ipython_extension(ipython) -> None:
    """``%load_ext llmlab.complete`` で %%complete マジックを登録する。"""
    from IPython.core.magic import Magics, line_cell_magic, magics_class

    @magics_class
    class _CompleteMagics(Magics):
        @line_cell_magic
        def complete(self, line: str, cell: str | None = None):
            """``%%complete`` でセルのコードを補完する。

            オプション: ``--lang <name>``（既定 python）, ``--no-insert``（次セル挿入なし）。
            既定では「元コード＋補完」を編集可能な新規セルとして下に挿入する。
            """
            opts = line.split()
            language = "python"
            if "--lang" in opts:
                i = opts.index("--lang")
                if i + 1 < len(opts):
                    language = opts[i + 1]
            insert = "--no-insert" not in opts

            prefix = cell if cell is not None else line
            if not prefix.strip():
                print("（補完するコードがありません）")
                return
            completion = code_complete(prefix, language=language)
            print(completion if completion else "（補完候補が空でした）")
            if insert and completion:
                self.shell.set_next_input(prefix + completion, replace=False)

    ipython.register_magics(_CompleteMagics)


def completion_panel(language: str = "python"):
    """入力欄＋「補完」ボタンのコード補完 UI（ipywidgets）を表示する。"""
    from .config import widget_env

    ok, reason = widget_env()
    if not ok:
        print(f"completion_panel はブラウザのノートブック上の ipywidgets が必要です（{reason}）。")
        print("代わりに次を使ってください: %load_ext llmlab.complete → %%complete、"
              "もしくは llmlab.code_complete('コード')。診断は llmlab.doctor()。")
        return

    import ipywidgets as widgets
    from IPython.display import display

    code = widgets.Textarea(
        placeholder="ここにコードを書いて『補完』を押す",
        layout=widgets.Layout(width="100%", height="160px"),
    )
    lang = widgets.Text(value=language, description="Lang", layout=widgets.Layout(width="200px"))
    button = widgets.Button(description="補完", button_style="primary")
    result = widgets.Textarea(
        description="結果", layout=widgets.Layout(width="100%", height="200px")
    )
    status = widgets.Output()

    def _on_click(_):
        status.clear_output()
        with status:
            if not code.value.strip():
                print("コードを入力してください")
                return
            try:
                completion = code_complete(code.value, language=lang.value.strip() or "python")
                result.value = code.value + completion
                print("✅ 補完しました（下の『結果』を編集・コピーして使えます）")
            except Exception as e:  # noqa: BLE001
                print(f"❌ {e}")

    button.on_click(_on_click)
    display(widgets.VBox([
        widgets.HTML("<b>コード補完</b>（OpenAI 互換エンドポイント）"),
        code, lang, button, result, status,
    ]))
