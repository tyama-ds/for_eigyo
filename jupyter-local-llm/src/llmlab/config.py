"""接続設定の一元管理（セッション内で入力する方式）。

`.env` やファイルからは読み込まない。ノートブック内で明示的に設定する::

    import llmlab
    llmlab.configure(
        base_url="http://localhost:8000/v1",
        api_key="...",
        model="your-model-name",
        embed_model="your-embedding-model",   # 省略時は model を流用
    )

または JupyterLab 上の入力フォームで設定する::

    llmlab.settings_form()    # ボタンを押すと configure() が走る
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """エンドポイント接続設定。"""

    base_url: str
    api_key: str
    model: str
    embed_model: str
    context_window: int = 8192


# セッション内で保持する現在の設定（プロセス内メモリのみ。永続化しない）。
_current: Settings | None = None


def configure(
    base_url: str,
    api_key: str,
    model: str,
    *,
    embed_model: str | None = None,
    context_window: int = 8192,
) -> Settings:
    """接続情報を設定する。チャット / 補完 / RAG はこの値を共有する。"""
    global _current

    missing = [
        name
        for name, value in {"base_url": base_url, "api_key": api_key, "model": model}.items()
        if not value
    ]
    if missing:
        raise ValueError("必須項目が未入力です: " + ", ".join(missing))

    _current = Settings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        embed_model=embed_model or model,
        context_window=context_window,
    )

    # 設定変更を依存モジュールに反映（生成済みクライアントを破棄）。
    from . import client

    client.reset_client()
    return _current


def get_settings() -> Settings:
    """現在の設定を返す。未設定なら例外。"""
    if _current is None:
        raise RuntimeError(
            "接続設定が未入力です。\n"
            "llmlab.configure(base_url=..., api_key=..., model=...) を実行するか、\n"
            "llmlab.settings_form() でフォームから入力してください。"
        )
    return _current


def is_configured() -> bool:
    return _current is not None


def settings_form():
    """JupyterLab 上で接続情報を入力するフォーム（ipywidgets）を表示する。"""
    import ipywidgets as widgets
    from IPython.display import display

    base_url = widgets.Text(
        description="Base URL", placeholder="http://localhost:8000/v1",
        layout=widgets.Layout(width="480px"),
    )
    api_key = widgets.Password(description="API Key", layout=widgets.Layout(width="480px"))
    model = widgets.Text(description="Model", placeholder="your-model-name",
                         layout=widgets.Layout(width="480px"))
    embed_model = widgets.Text(description="Embed Model", placeholder="（省略可: Model を流用）",
                               layout=widgets.Layout(width="480px"))
    context_window = widgets.IntText(description="Ctx Window", value=8192)
    button = widgets.Button(description="設定を適用", button_style="primary")
    status = widgets.Output()

    # 既存値があればフォームに反映（api_key は安全のため再入力させる）。
    if _current is not None:
        base_url.value = _current.base_url
        model.value = _current.model
        embed_model.value = "" if _current.embed_model == _current.model else _current.embed_model
        context_window.value = _current.context_window

    def _on_click(_):
        status.clear_output()
        with status:
            try:
                s = configure(
                    base_url=base_url.value.strip(),
                    api_key=api_key.value,
                    model=model.value.strip(),
                    embed_model=embed_model.value.strip() or None,
                    context_window=context_window.value,
                )
                print(f"✅ 設定しました: {s.base_url}  model={s.model}  embed={s.embed_model}")
            except Exception as e:  # noqa: BLE001
                print(f"❌ {e}")

    button.on_click(_on_click)
    display(
        widgets.VBox([base_url, api_key, model, embed_model, context_window, button, status])
    )
