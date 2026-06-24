"""JupyterLab インライン補完拡張（プレビルト・フロントエンドのみ）。

補完ロジックはアクティブなカーネル内の ``llmlab.inline_complete`` を呼ぶため、
本 Python パッケージは labextension の登録のみを行う。
"""

__version__ = "0.1.0"


def _jupyter_labextension_paths():
    return [{"src": "labextension", "dest": "jupyterlab-llmlab-completer"}]
