"""接続設定の一元管理（セッション内で入力する方式）。

`.env` やファイルからは読み込まない。ノートブック内で明示的に設定する::

    import llmlab
    llmlab.configure(
        base_url="http://localhost:8000/v1",
        api_key="...",
        model="your-model-name",
        embed_model="your-embedding-model",   # 省略時は model を流用
        use_proxy=True,                        # プロキシ経由にするか
        proxy_url="http://proxy.example:8080", # 省略時は環境変数のプロキシを使用
    )

または JupyterLab 上の入力フォームで設定する::

    llmlab.settings_form()    # ボタンを押すと configure() が走る
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """エンドポイント接続設定。"""

    base_url: str
    api_key: str = field(repr=False)  # repr 経由でノートブック出力(.ipynb)へ漏れないように
    model: str = ""
    embed_model: str = ""
    context_window: int = 8192
    request_timeout: float = 120.0  # 1リクエストの上限秒。遅い/固まるサーバ対策
    use_proxy: bool = False
    proxy_url: str | None = None
    # 埋め込み（RAG/BookRAG 用）。チャットと別エンドポイントの場合に指定する。
    # 省略時は base_url / api_key を流用。
    embed_base_url: str | None = None
    embed_api_key: str | None = field(default=None, repr=False)
    # "openai": OpenAI 互換エンドポイントの /v1/embeddings を使う
    # "local" : ローカルの HuggingFace 埋め込みを使う（サーバに embeddings API が無い時）
    embed_provider: str = "openai"
    embed_local_model: str = "intfloat/multilingual-e5-small"


# セッション内で保持する現在の設定（プロセス内メモリのみ。永続化しない）。
_current: Settings | None = None


def configure(
    base_url: str,
    api_key: str,
    model: str,
    *,
    embed_model: str | None = None,
    context_window: int = 8192,
    request_timeout: float = 120.0,
    use_proxy: bool = False,
    proxy_url: str | None = None,
    embed_base_url: str | None = None,
    embed_api_key: str | None = None,
    embed_provider: str = "openai",
    embed_local_model: str = "intfloat/multilingual-e5-small",
) -> Settings:
    """接続情報を設定する。チャット / 補完 / RAG はこの値を共有する。

    use_proxy=False のときは環境変数のプロキシも無視して直結する。
    use_proxy=True で proxy_url 未指定なら、環境変数（HTTP(S)_PROXY）のプロキシを使う。

    埋め込み（RAG）について:
    - サーバの埋め込みが別エンドポイントなら embed_base_url（必要なら embed_api_key）を指定。
    - サーバに /v1/embeddings が無いなら embed_provider="local" でローカル埋め込みを使う
      （`pip install llama-index-embeddings-huggingface` が必要）。
    """
    global _current

    missing = [
        name
        for name, value in {"base_url": base_url, "api_key": api_key, "model": model}.items()
        if not value
    ]
    if missing:
        raise ValueError("必須項目が未入力です: " + ", ".join(missing))

    # 型・値の軽い検証（後段の分かりにくいエラーをここで止める）
    try:
        context_window = int(context_window)
        request_timeout = float(request_timeout)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"context_window / request_timeout は数値で指定してください: {e}"
        ) from e
    if context_window <= 0 or request_timeout <= 0:
        raise ValueError("context_window / request_timeout は正の値で指定してください")
    if embed_provider not in ("openai", "local"):
        raise ValueError('embed_provider は "openai" か "local" を指定してください')

    _current = Settings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        embed_model=embed_model or model,
        context_window=context_window,
        request_timeout=request_timeout,
        use_proxy=use_proxy,
        proxy_url=(proxy_url or None) if use_proxy else None,
        embed_base_url=embed_base_url or None,
        embed_api_key=embed_api_key or None,
        embed_provider=embed_provider,
        embed_local_model=embed_local_model,
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


def in_notebook() -> bool:
    """ブラウザのノートブック（ZMQ）カーネル上で動いているかを判定する。

    ターミナル IPython / 素の python / nbconvert などでは ipywidgets は描画できない。
    """
    try:
        from IPython import get_ipython

        ip = get_ipython()
        return ip is not None and ip.__class__.__name__ == "ZMQInteractiveShell"
    except Exception:  # noqa: BLE001
        return False


def widget_env() -> tuple[bool, str]:
    """ipywidgets が描画できそうな環境かを (ok, 理由) で返す。"""
    try:
        import ipywidgets  # noqa: F401
    except ImportError:
        return False, "ipywidgets が未インストールです"
    if not in_notebook():
        return False, "ブラウザのノートブック（カーネル）上ではありません"
    return True, ""


def doctor() -> None:
    """環境診断。ウィジェットが表示されない（VBox がテキスト表示される）原因を切り分ける。"""
    import importlib
    import sys

    print("=== llmlab doctor ===")
    print("Python           :", sys.version.split()[0])
    print("In notebook kernel:", in_notebook())
    print("接続設定済み      :", is_configured())
    print("--- 依存パッケージ ---")
    core = ("ipywidgets", "jupyterlab", "openai", "httpx", "numpy", "pypdf", "tqdm",
            "llama_index.core")
    optional = ("pandas", "pdfplumber", "docx", "openpyxl", "pptx",
                "fitz", "pytesseract", "RestrictedPython", "sentence_transformers")
    for pkg in core:
        try:
            m = importlib.import_module(pkg)
            print(f"  {pkg:18s}: {getattr(m, '__version__', '?')}")
        except Exception:  # noqa: BLE001
            print(f"  {pkg:18s}: ✗ 未インストール")
    print("--- 任意依存（該当機能を使う場合のみ必要） ---")
    for pkg in optional:
        try:
            m = importlib.import_module(pkg)
            print(f"  {pkg:18s}: {getattr(m, '__version__', 'OK')}")
        except Exception:  # noqa: BLE001
            print(f"  {pkg:18s}: -（未導入）")
    ok, reason = widget_env()
    print("--- ウィジェット描画 ---")
    if ok:
        print("  描画できる見込み: OK")
    else:
        print(f"  描画できません: {reason}")
        print("  対処:")
        print("   - ブラウザで `jupyter lab` を起動し、そのノートブックのセルで実行する")
        print("   - VBox がテキスト表示される場合、ipywidgets がカーネルと別環境の可能性。")
        print("     `pip install -e .` した環境で jupyter lab を起動し、カーネルを再起動する")
        print("   - フォーム無しで設定するなら: llmlab.settings_form(text=True) または")
        print("     llmlab.configure(base_url=..., api_key=..., model=...)")


def _settings_form_text() -> "Settings":
    """ウィジェットが使えない環境向けのテキスト入力フォールバック。"""
    import getpass

    print("接続情報を入力してください（ウィジェット非対応環境のためテキスト入力）。")
    base_url = input("Base URL (例 http://localhost:8000/v1): ").strip()
    api_key = getpass.getpass("API Key: ")
    model = input("Model: ").strip()
    embed_model = input("Embed Model（空なら Model を流用）: ").strip() or None
    use_proxy = input("プロキシを使う? [y/N]: ").strip().lower() == "y"
    proxy_url = None
    if use_proxy:
        proxy_url = input("Proxy URL（空なら環境変数を使用）: ").strip() or None
    s = configure(base_url=base_url, api_key=api_key, model=model,
                  embed_model=embed_model, use_proxy=use_proxy, proxy_url=proxy_url)
    proxy = (s.proxy_url or "環境変数") if s.use_proxy else "なし（直結）"
    print(f"✅ 設定しました: {s.base_url}  model={s.model}  proxy={proxy}")
    return s


def settings_form(text: bool = False):
    """JupyterLab 上で接続情報を入力するフォーム（ipywidgets）を表示する。

    ウィジェットが描画できない環境（ターミナル/別環境の ipywidgets 等）では自動で
    テキスト入力にフォールバックする。``text=True`` で明示的にテキスト入力にできる。
    """
    ok, reason = widget_env()
    if text or not ok:
        if not text:
            print(f"[フォールバック] {reason}。テキスト入力に切り替えます。"
                  "（フォームを使うには下の『対処』を参照: llmlab.doctor()）")
        return _settings_form_text()

    import ipywidgets as widgets
    from IPython.display import display

    field_layout = widgets.Layout(width="480px")

    base_url = widgets.Text(
        description="Base URL", placeholder="http://localhost:8000/v1", layout=field_layout,
    )
    api_key = widgets.Password(description="API Key", layout=field_layout)
    model = widgets.Text(description="Model", placeholder="your-model-name", layout=field_layout)
    embed_model = widgets.Text(
        description="Embed Model", placeholder="（省略可: Model を流用）", layout=field_layout,
    )
    context_window = widgets.IntText(description="Ctx Window", value=8192)

    # --- プロキシ設定（on/off + URL） ---
    use_proxy = widgets.Checkbox(description="プロキシを使う", value=False, indent=False)
    proxy_url = widgets.Text(
        description="Proxy URL", placeholder="http://proxy:8080（空なら環境変数を使用）",
        layout=field_layout, disabled=True,
    )

    def _toggle_proxy(change):
        proxy_url.disabled = not change["new"]

    use_proxy.observe(_toggle_proxy, names="value")

    # --- 埋め込み（RAG）設定 ---
    embed_base_url = widgets.Text(
        description="Embed URL", placeholder="空ならBase URLを流用。別エンドポイントなら指定",
        layout=field_layout,
    )
    embed_local = widgets.Checkbox(
        description="ローカル埋め込みを使う（サーバに /v1/embeddings が無い場合）",
        value=False, indent=False,
    )

    button = widgets.Button(description="設定を適用", button_style="primary")
    status = widgets.Output()

    # 既存値があればフォームに反映（api_key は安全のため再入力させる）。
    if _current is not None:
        base_url.value = _current.base_url
        model.value = _current.model
        embed_model.value = "" if _current.embed_model == _current.model else _current.embed_model
        context_window.value = _current.context_window
        use_proxy.value = _current.use_proxy
        proxy_url.value = _current.proxy_url or ""
        proxy_url.disabled = not _current.use_proxy
        embed_base_url.value = _current.embed_base_url or ""
        embed_local.value = _current.embed_provider == "local"

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
                    use_proxy=use_proxy.value,
                    proxy_url=proxy_url.value.strip() or None,
                    embed_base_url=embed_base_url.value.strip() or None,
                    embed_provider="local" if embed_local.value else "openai",
                )
                proxy = (s.proxy_url or "環境変数") if s.use_proxy else "なし（直結）"
                embed = ("ローカル:" + s.embed_local_model) if s.embed_provider == "local" \
                    else (s.embed_base_url or "Base URL 流用")
                print(f"✅ 設定しました: {s.base_url}  model={s.model}  proxy={proxy}  embed={embed}")
            except Exception as e:  # noqa: BLE001
                print(f"❌ {e}")

    button.on_click(_on_click)
    display(
        widgets.VBox(
            [
                widgets.HTML("<b>ローカルLLM 接続設定</b>（OpenAI 互換エンドポイント）"),
                base_url,
                api_key,
                model,
                embed_model,
                context_window,
                widgets.HTML("<hr style='margin:6px 0'><b>埋め込み（RAG）</b>"),
                embed_base_url,
                embed_local,
                widgets.HTML("<hr style='margin:6px 0'><b>プロキシ</b>"),
                use_proxy,
                proxy_url,
                button,
                status,
            ]
        )
    )
    print("※ 上にフォームが表示されない（VBox(...) と出る）場合は llmlab.doctor() で診断、"
          "または llmlab.settings_form(text=True) を使ってください。")
