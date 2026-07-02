"""TableQA — 表データ（Excel/CSV/DataFrame）への自然言語質問を pandas で解く（text-to-pandas）。

RAG（検索）が苦手な「集計・計算・条件抽出」を担当する。質問から pandas コードを LLM に
生成させ、制限付きの名前空間で実行して答えを返す。

使い方::

    import llmlab
    llmlab.configure(...)                    # 接続設定（チャットAPI のみで動く）

    tq = llmlab.TableQA("売上.xlsx")          # CSV / DataFrame / {name: DataFrame} も可
    ans = tq.ask("東京支店の4月の売上合計は？")
    print(ans)          # 回答（自然言語）＋ 生成コード ＋ 実行結果
    ans.result          # 計算結果そのもの（DataFrame/数値など）
    tq.tables           # 読み込んだ DataFrame 群（dict）

注意: LLM 生成コードを実行するため、`__`/import/os 等の危険トークンを含むコードは拒否し、
builtins を最小化した名前空間で実行する（完全なサンドボックスではない。信頼できる
ローカル環境での利用を想定）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .client import complete

_FORBIDDEN = (
    "__", "import", "open(", "eval(", "exec(", "compile(", "os.", "sys.",
    "subprocess", "socket", "shutil", "pathlib", "globals(", "locals(",
    "getattr(", "setattr(", "delattr(", "input(", "exit(", "quit(",
    # pandas の I/O 面（ファイル読み書き・SSRF・pickle 実行）を塞ぐ。
    # データは名前空間の DataFrame として渡すため、生成コードに読込は不要。
    "read_", "pd.io", "pickle", "urlopen", "storage_options",
    "to_csv", "to_excel", "to_parquet", "to_json", "to_hdf", "to_sql",
    "to_feather", "to_stata", "to_clipboard", "to_latex", "to_html",
    "to_markdown", "buf=", "path_or_buf",
)

_SAFE_BUILTINS = {
    "len": len, "range": range, "sum": sum, "min": min, "max": max, "abs": abs,
    "round": round, "sorted": sorted, "list": list, "dict": dict, "set": set,
    "tuple": tuple, "float": float, "int": int, "str": str, "bool": bool,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter, "any": any,
    "all": all, "print": print,
}


@dataclass
class TableAnswer:
    question: str
    code: str
    result: object
    answer: str = ""

    def __str__(self) -> str:
        parts = [self.answer.strip()] if self.answer.strip() else []
        parts.append("── 生成コード ──\n" + self.code.strip())
        parts.append("── 実行結果 ──\n" + str(self.result))
        return "\n\n".join(parts)


class TableQA:
    """表データへの自然言語質問を pandas で解く。"""

    def __init__(self, source=None, *, sheet=None):
        self.tables: dict = self._load(source, sheet)
        if not self.tables:
            raise ValueError("表データが空です。ファイルパス / DataFrame / {名前:DataFrame} を渡してください。")

    # ---- 読み込み ----
    def _load(self, source, sheet) -> dict:
        import pandas as pd

        if source is None:
            return {}
        if isinstance(source, pd.DataFrame):
            return {"df": source}
        if isinstance(source, dict):
            return dict(source)
        p = Path(source)
        suffix = p.suffix.lower()
        if suffix == ".csv":
            return {p.stem: pd.read_csv(p)}
        if suffix in (".xlsx", ".xls"):
            data = pd.read_excel(p, sheet_name=sheet)  # sheet=None → 全シート dict
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items()}
            return {sheet or p.stem: data}
        raise ValueError(f"未対応の形式です: {suffix}（.csv / .xlsx / DataFrame を使用）")

    # ---- スキーマ記述（LLM への文脈） ----
    def _schema(self) -> tuple[str, dict]:
        varmap = {}
        lines = []
        single = len(self.tables) == 1
        for name, df in self.tables.items():
            var = "df" if single else "df_" + re.sub(r"[^\w]+", "_", str(name)).strip("_")
            if var in varmap:  # サニタイズ後の名前衝突は連番で回避（表を落とさない）
                i = 2
                while f"{var}_{i}" in varmap:
                    i += 1
                var = f"{var}_{i}"
            varmap[var] = df
            cols = ", ".join(f"{c}({df[c].dtype})" for c in df.columns)
            try:
                head = df.head(3).to_csv(index=False)
            except Exception:  # noqa: BLE001
                head = "(preview 不可)"
            lines.append(f"- 変数 `{var}` (shape={df.shape})\n  列: {cols}\n  先頭3行:\n{head}")
        return "\n".join(lines), varmap

    # ---- コード生成 ----
    def code(self, question: str) -> str:
        schema, _ = self._schema()
        prompt = (
            "あなたは pandas の専門家です。以下の DataFrame 群を使い、質問に答える Python コードを"
            "書いてください。ルール:\n"
            "- pandas は `pd` として import 済み。追加の import は禁止。\n"
            "- 提供された変数のみ使用。最終的な答えを変数 `result` に代入する。\n"
            "- コードのみを出力（説明・Markdown フェンス不要）。\n\n"
            f"# 利用可能なデータ\n{schema}\n\n# 質問\n{question}"
        )
        return _strip_code(complete(prompt))

    # ---- 実行（サンドボックス経由） ----
    def _run(self, code: str):
        import pandas as pd

        from .sandbox import safe_exec

        _, varmap = self._schema()
        # コピーを渡し、生成コードの in-place 変更が元データへ波及しないようにする
        namespace = {"pd": pd, **{k: v.copy() for k, v in varmap.items()}}
        # RestrictedPython（AST制限）＋ deny-list（I/O名）の多重防御
        return safe_exec(code, namespace, _FORBIDDEN, result_var="result")

    # ---- 質問 ----
    def ask(self, question: str, *, explain: bool = True) -> TableAnswer:
        code = self.code(question)
        result = self._run(code)
        answer = self._explain(question, result) if explain else ""
        return TableAnswer(question=question, code=code, result=result, answer=answer)

    def _explain(self, question: str, result) -> str:
        text = str(result)
        if len(text) > 2000:
            text = text[:2000] + " …(以下略)"
        prompt = (
            "次は表データに対する計算結果です。質問に日本語で簡潔に答えてください"
            "（数値はそのまま引用）。\n\n"
            f"質問: {question}\n計算結果:\n{text}"
        )
        return complete(prompt).strip()


def _strip_code(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_+-]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()
