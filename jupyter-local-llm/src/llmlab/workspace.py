"""MultiRAG — 別々に作った索引（PagedRAG / BookRAG）を横断して調べる。

「2014年フォルダの索引」「2015年フォルダの索引」のように別プロセス・別日に
作った索引フォルダを、あとから組み合わせて 検索 / 要約 / レポート / 数値抽出
の対象にできる。索引はフォルダ単位で自己完結しているため、追加の変換は不要。

使い方::

    import llmlab
    llmlab.configure(...)                      # 索引を作ったときと同じ埋め込みモデルで

    ws = llmlab.MultiRAG(["./storage/2014", "./storage/2015"])
    print(ws.ask("売上高の推移は？"))            # 横断QA（索引ごとに調べて突き合わせ）
    print(ws.summarize())                       # 横断要約
    print(ws.report("年度比較レポート"))         # Markdown レポート生成
    ex = ws.extract("各年の売上高と利益")        # 数値抽出（表・グラフ用）
    ex.to_df()                                  # pandas.DataFrame

    llmlab.MultiRAG.discover("./storage")       # フォルダ内の索引を自動検出

ワンストップUI（ブラウザ）は ``python -m llmlab.app`` で起動する。

注意: 組み合わせる索引は **同じ埋め込みモデル** で作られている必要がある
（ベクトル空間が違うと検索精度が壊れる）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import bookindex as bx

DEFAULT_ROOT = "./storage"


# --------------------------------------------------------------------------
# ピン留め（よく使う索引フォルダの記憶）
#
# 接続情報（APIキー等）はセッション内のみだが、ピンは秘匿情報ではない
# フォルダパスなので ~/.llmlab/pins.json に永続化する（再起動後も残る）。
# --------------------------------------------------------------------------

LLMLAB_DIR = Path.home() / ".llmlab"
PINS_PATH = LLMLAB_DIR / "pins.json"


def _read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_file(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def pinned_paths() -> list[str]:
    """ピン留め済みフォルダの絶対パス一覧。"""
    raw = _read_json_file(PINS_PATH, [])
    return [str(p) for p in raw if isinstance(p, str)] if isinstance(raw, list) else []


def pin_index(path: str | Path) -> list[str]:
    """索引フォルダをピン留めする（重複は無視）。ピン一覧を返す。"""
    p = str(Path(path).resolve())
    pins = pinned_paths()
    if p not in pins:
        pins.append(p)
        _write_json_file(PINS_PATH, pins)
    return pins


def unpin_index(path: str | Path) -> list[str]:
    """ピン留めを外す。ピン一覧を返す。"""
    p = str(Path(path).resolve())
    pins = [x for x in pinned_paths() if x != p]
    _write_json_file(PINS_PATH, pins)
    return pins


def pinned_indexes() -> list[IndexInfo]:
    """ピン留め済みの索引を IndexInfo で返す。

    フォルダが消えている/索引でなくなっている場合は kind="missing" で返す
    （一覧から気づいて外せるように、黙って消さない）。
    """
    out = []
    for p in pinned_paths():
        info = detect_index(p)
        if info is None:
            info = IndexInfo(path=p, name=Path(p).name, kind="missing")
        info.pinned = True
        out.append(info)
    return out


# --------------------------------------------------------------------------
# 索引の検出
# --------------------------------------------------------------------------


@dataclass
class IndexInfo:
    """索引フォルダ1つ分のメタ情報。"""

    path: str
    name: str            # 表示名（フォルダ名。例: "2014"）
    kind: str            # "paged" | "book" | "missing"（ピン留め先が消えた場合）
    books: list[str] = field(default_factory=list)
    units: int = 0       # paged: チャンク数 / book: ノード数
    pinned: bool = False  # ピン留め（~/.llmlab/pins.json に永続化）

    def to_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "kind": self.kind,
                "books": self.books, "units": self.units, "pinned": self.pinned}


def detect_index(path: str | Path) -> IndexInfo | None:
    """フォルダが llmlab の索引なら IndexInfo を返す（違えば None）。"""
    d = Path(path)
    if not d.is_dir():
        return None
    if (d / "bookindex.json").exists():
        try:
            payload = json.loads((d / "bookindex.json").read_text(encoding="utf-8"))
            roots = set(payload.get("roots", []))
            books = [n.get("title") or n.get("book") or "?"
                     for n in payload.get("nodes", []) if n.get("id") in roots]
            return IndexInfo(path=str(d), name=d.name, kind="book",
                             books=books, units=len(payload.get("nodes", [])))
        except Exception:  # noqa: BLE001  壊れた索引は一覧に出さない
            return None
    if (d / "docstore.json").exists():
        books, units = [], 0
        cat = d / "books.json"
        if cat.exists():
            try:
                entries = json.loads(cat.read_text(encoding="utf-8"))
                books = [b.get("title", "?") for b in entries]
                units = sum(int(b.get("chunks", 0)) for b in entries)
            except Exception:  # noqa: BLE001
                pass
        return IndexInfo(path=str(d), name=d.name, kind="paged", books=books, units=units)
    return None


def _is_manager_root(p: Path) -> bool:
    """IndexManager（doc_id 中心レジストリ）の保存ルートか。

    内部の vectors/ を通常の paged 索引として索引横断ビューに露出させると、
    そこへ旧経路で追記されてレジストリの整合（docs/ メタ）が壊れるため除外する。
    """
    return (p / "docs").is_dir() and (p / "status").is_dir() and (p / "vectors").is_dir()


def discover(root: str | Path = DEFAULT_ROOT, *, include_pins: bool = True) -> list[IndexInfo]:
    """root 直下（と root 自身）から索引フォルダを検出する。

    include_pins=True（既定）なら、ピン留め済みの索引を root の内外を問わず
    先頭に含める（root 内で見つかったものにはピン印を付ける）。
    IndexManager の内部ストア（index/vectors 等）は対象外（文書管理ビューで扱う）。
    """
    root = Path(root)
    found: list[IndexInfo] = []
    if root.exists():
        info = detect_index(root)
        if info:
            found.append(info)
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if _is_manager_root(d) or (_is_manager_root(root)
                                       and d.name in ("vectors", "bookindex")):
                continue  # IndexManager 領域は露出しない
            info = detect_index(d)
            if info:
                found.append(info)
            else:  # bookindex 既定の入れ子（storage/bookindex 等）も1段だけ見る
                for dd in sorted(p for p in d.iterdir() if p.is_dir()):
                    if _is_manager_root(dd):
                        continue
                    sub = detect_index(dd)
                    if sub:
                        sub.name = f"{d.name}/{dd.name}"
                        found.append(sub)
    if not include_pins:
        return found
    # ピンを合流: root 内で検出済みのものは印だけ、root 外のピンは追加。ピンを先頭に
    pins = pinned_indexes()
    by_path = {str(Path(f.path).resolve()): f for f in found}
    merged_pins = []
    for pin in pins:
        hit = by_path.pop(str(Path(pin.path).resolve()), None)
        if hit is not None:
            hit.pinned = True
            merged_pins.append(hit)
        else:
            merged_pins.append(pin)  # root 外 or missing
    return merged_pins + [f for f in found if not f.pinned]


# --------------------------------------------------------------------------
# 索引の作成（Studio の「索引を作成」からも使う）
# --------------------------------------------------------------------------

# PagedRAG.add_books と同じ対応拡張子
_BUILD_EXTS_PAGED = {".pdf", ".txt", ".md", ".docx", ".doc", ".pptx",
                     ".csv", ".xlsx", ".xls", ".html", ".epub"}
# BookRAG.add_book が受け付けるもの
_BUILD_EXTS_BOOK = {".pdf", ".docx", ".md", ".txt", ".pptx", ".xlsx"}


def _make_builder(kind: str, storage_dir: Path, *, max_workers: int = 1):
    """kind に応じた取り込みエンジンを作る（テストで差し替えられるよう分離）。"""
    if kind == "book":
        from .bookrag import BookRAG

        return BookRAG(storage_dir=storage_dir, max_workers=max_workers)
    from .pagedrag import PagedRAG

    return PagedRAG(storage_dir=storage_dir)


def build_index(docs_path: str | Path, storage_dir: str | Path, *,
                kind: str = "paged", layout: bool = False, ocr: bool = False,
                max_workers: int = 1, pin: bool = False, progress=None) -> IndexInfo:
    """フォルダ（またはファイル1つ）を取り込んで索引フォルダを作る。

    - kind="paged": フォルダ内の文書をまとめてベクトル索引に（推奨・高速）
    - kind="book" : 各文書を BookRAG（木+知識グラフ）で深掘り索引に（低速）
    - 既存の同種索引フォルダを指定した場合は **追記**（同名ファイルはスキップ）
    - progress: callable({"stage","current","total","detail"})。省略時は log 表示
    - pin=True で作成後にピン留めする
    """
    if kind not in ("paged", "book"):
        raise ValueError('kind は "paged" か "book" を指定してください')
    docs = Path(docs_path)
    if not docs.exists():
        raise FileNotFoundError(f"文書のパスが見つかりません: {docs}")
    exts = _BUILD_EXTS_BOOK if kind == "book" else _BUILD_EXTS_PAGED
    if docs.is_file():
        files = [docs]
    else:
        files = sorted(f for f in docs.iterdir()
                       if f.is_file() and f.suffix.lower() in exts)
    if not files:
        raise FileNotFoundError(
            f"{docs} に対応ファイルがありません（対応: {', '.join(sorted(exts))}）")

    storage_dir = Path(storage_dir)
    existing = detect_index(storage_dir)
    if existing and existing.kind != kind:
        raise ValueError(
            f"{storage_dir} は既存の {existing.kind} 索引です。"
            f"{kind} で作り直す場合は別の索引名にするか、フォルダを削除してください")

    def emit(stage: str, current: int, total: int, detail: str = "") -> None:
        if progress is not None:
            try:
                progress({"stage": stage, "current": current,
                          "total": total, "detail": detail})
            except Exception:  # noqa: BLE001
                pass
        else:
            bx.log(f"({min(current + 1, total)}/{total}) {stage}"
                   + (f" — {detail}" if detail else ""))

    builder = _make_builder(kind, storage_dir, max_workers=max_workers)
    total = len(files)
    if existing:
        emit("既存索引へ追記", 0, total, f"{existing.name}（同名ファイルはスキップ）")
    if kind == "book" and total > 1:
        emit("注意", 0, total,
             "BOOK 方式は1文書ごとに知識グラフを構築するため時間がかかります")

    # BookRAG の内部ログ（[1/5] 解析…等）も進捗として転送する。
    # bx.log_to はスレッドローカルのため、Studio で他タスクと並行しても混線しない
    # （旧実装の bx.log グローバル差し替えは並行実行で競合していた）。
    from contextlib import nullcontext

    state = {"i": 0, "file": ""}

    def _forward_log(msg):
        emit(f"取り込み: {state['file']}", state["i"], total, str(msg))

    with (bx.log_to(_forward_log) if progress is not None else nullcontext()):
        for i, f in enumerate(files):
            state["i"], state["file"] = i, f.name
            emit(f"取り込み: {f.name}", i, total, f"{i + 1}/{total} 件目（{kind}）")
            if kind == "book":
                builder.add_book(f, layout=("auto" if layout else False), ocr=ocr)
            else:
                builder.add_book(f)

    if pin:
        pin_index(storage_dir)
    info = detect_index(storage_dir)
    if info is None:  # 全ファイルがスキップ等で索引が出来なかった場合
        raise RuntimeError(f"索引を作成できませんでした: {storage_dir}")
    info.pinned = str(storage_dir.resolve()) in pinned_paths()
    emit("完了", total, total,
         f"{info.name}: 文書 {len(info.books)} 冊 / {info.units} units")
    return info


# --------------------------------------------------------------------------
# 結果の型
# --------------------------------------------------------------------------


@dataclass
class Partial:
    """索引1つ分の部分回答。"""

    index: str
    kind: str
    text: str
    sources: list[str] = field(default_factory=list)
    # 出典リンク: {"label": 表示名, "path": 元ファイル絶対パス, "page": ページ}
    refs: list[dict] = field(default_factory=list)


@dataclass
class MultiAnswer:
    """横断処理の最終結果。text は回答/要約/レポート本文（Markdown 可）。"""

    text: str
    partials: list[Partial] = field(default_factory=list)

    def refs(self) -> list[dict]:
        """全索引の出典リンク（絶対パス）を重複除去して返す。"""
        seen, out = set(), []
        for p in self.partials:
            for r in p.refs:
                key = (r.get("path"), r.get("page"))
                if key in seen:
                    continue
                seen.add(key)
                out.append({**r, "index": p.index})
        return out

    def __str__(self) -> str:
        out = [self.text]
        refs = self.refs()
        if refs:
            out += ["", "── リファレンス（該当箇所） ──"]
            for r in refs[:20]:
                page = f" p.{r['page']}" if r.get("page") else ""
                path = r.get("path") or "(パス不明: 旧バージョンで作成した索引)"
                out.append(f"- [{r['index']}] {r.get('label', '?')}{page}\n    ↳ {path}")
        elif any(p.sources for p in self.partials):
            out += ["", "── 出典 ──"] + [s for p in self.partials for s in p.sources][:20]
        return "\n".join(out)


@dataclass
class ExtractResult:
    """数値抽出の結果。rows は {item, value, unit, index, source} の列。"""

    rows: list[dict]
    note: str = ""
    partials: list[Partial] = field(default_factory=list)

    def to_df(self):
        import pandas as pd

        return pd.DataFrame(self.rows)

    def __str__(self) -> str:
        lines = [f"- {r.get('item','?')}: {r.get('value','?')} {r.get('unit','') or ''}"
                 f"（{r.get('index','?')} / {r.get('source','') or '出典不明'}）"
                 for r in self.rows]
        return "\n".join(lines + ([self.note] if self.note else []))


# --------------------------------------------------------------------------
# MultiRAG 本体
# --------------------------------------------------------------------------

_REDUCE_ASK = (
    "あなたは複数の索引（文書群）を横断して調べるアシスタントです。"
    "以下は索引ごとの調査結果です。これらを突き合わせ、質問に日本語で答えてください。"
    "索引間で値や記述が異なる場合は、どの索引（例: 年度）の情報かを必ず明示してください。"
    "調査結果に無い内容は推測しないでください。"
)

_REDUCE_SUMMARY = (
    "以下は索引（文書群）ごとの要約です。全体を貫く共通点・相違点・変化が分かるように、"
    "日本語で統合要約を書いてください。索引名（例: 年度）を明示しながらまとめてください。"
)

_REDUCE_REPORT = (
    "以下は索引（文書群）ごとの調査結果です。これを基に Markdown 形式のレポートを"
    "日本語で作成してください。構成: # タイトル / ## 概要（3行以内） / "
    "## 索引ごとの所見（索引名を小見出しに） / ## 横断比較（可能なら表） / ## 結論。"
    "調査結果に無い内容は書かないでください。"
)

_EXTRACT_MAP_Q = (
    "{question}。関連する数値・数量をできるだけ多く、"
    "それぞれ 名称・値・単位・出典（ページ等）が分かる形で列挙してください。"
)

_EXTRACT_REDUCE = (
    "以下は索引ごとの調査結果です。質問に関係する数値を抽出し、次の形式の JSON で"
    "返してください（JSON 以外は出力しない）:\n"
    '{"rows": [{"item": "指標名", "value": 数値, "unit": "単位", '
    '"index": "索引名", "source": "出典（文書名やページ）"}], "note": "補足があれば"}\n'
    "- value は数値型（カンマや単位を含めない）。読み取れないものは含めない。\n"
    "- index は必ず調査結果の索引名から選ぶ。"
)


class MultiRAG:
    """複数の索引フォルダを横断して 検索 / 要約 / レポート / 数値抽出 する。

    各索引はそれぞれの方式（PagedRAG=ベクトル検索 / BookRAG=エージェント検索）で
    調べ（Map）、結果を LLM で突き合わせて最終出力を作る（Reduce）。
    """

    def __init__(self, indexes: list, *, top_k: int = 5, progress=None):
        """indexes: 索引フォルダのパスの列（IndexInfo も可）。

        progress: 進捗コールバック callable(dict)。
        dict = {"stage": str, "current": int, "total": int, "detail": str}
        省略時は tqdm/print で表示する。
        """
        self._infos: list[IndexInfo] = []
        for item in indexes:
            info = item if isinstance(item, IndexInfo) else detect_index(item)
            if info is None or info.kind == "missing":
                raise FileNotFoundError(
                    f"索引が見つかりません: {item if info is None else info.path}\n"
                    "（PagedRAG は docstore.json、BookRAG は bookindex.json を含む"
                    "フォルダを指定してください）"
                )
            self._infos.append(info)
        if not self._infos:
            raise ValueError("索引を1つ以上指定してください")
        self.top_k = top_k
        self._progress_cb = progress
        self._engines: dict[str, object] = {}
        # 処理時間の見通しを警告する（索引ごとに順番に調査するため、ほぼ索引数に比例）
        n_book = sum(1 for i in self._infos if i.kind == "book")
        if len(self._infos) >= 3:
            bx.log(f"警告: 索引が {len(self._infos)} 個選択されています。"
                   "索引ごとに順番に調査するため、処理時間はほぼ索引数に比例して長くなります。")
        if n_book >= 1:
            bx.log(f"注意: BOOK 索引（{n_book} 個）はエージェント検索のため PAGED より"
                   "1索引あたりの時間が長くなります（ローカルLLMでは数十秒〜/索引）。")

    # ---- 情報 --------------------------------------------------------------

    def indexes(self) -> list[dict]:
        return [i.to_dict() for i in self._infos]

    @staticmethod
    def discover(root: str | Path = DEFAULT_ROOT) -> list[IndexInfo]:
        return discover(root)

    @classmethod
    def pinned(cls, **kwargs) -> "MultiRAG":
        """ピン留め済みの索引だけで MultiRAG を作る（消えたピンはスキップして通知）。"""
        infos = pinned_indexes()
        missing = [i.path for i in infos if i.kind == "missing"]
        for m in missing:
            bx.log(f"ピン留め先が見つからないためスキップ: {m}"
                   "（unpin_index() で外せます）")
        valid = [i for i in infos if i.kind != "missing"]
        if not valid:
            raise RuntimeError("有効なピン留め索引がありません。"
                               "llmlab.pin_index(パス) でピン留めしてください。")
        return cls(valid, **kwargs)

    # ---- 横断アクション ------------------------------------------------------

    def ask(self, question: str) -> MultiAnswer:
        """横断QA: 索引ごとに調べて突き合わせる。"""
        partials = self._map(question)
        text = self._reduce(_REDUCE_ASK, question, partials)
        return MultiAnswer(text=text, partials=partials)

    def summarize(self, instruction: str | None = None) -> MultiAnswer:
        """横断要約。instruction で観点を指定できる（例: 「リスク要因に注目」）。"""
        q = "この文書群の要点を、重要な数値・固有名詞を落とさず網羅的に要約してください。"
        if instruction:
            q += f" 特に次の観点を重視: {instruction}"
        partials = self._map(q)
        text = self._reduce(_REDUCE_SUMMARY, instruction or "全体要約", partials)
        return MultiAnswer(text=text, partials=partials)

    def report(self, topic: str) -> MultiAnswer:
        """Markdown レポートを生成する。"""
        partials = self._map(
            f"「{topic}」に関する情報（背景・数値・変化・特記事項）を詳しく調べてください。")
        text = self._reduce(_REDUCE_REPORT, topic, partials)
        return MultiAnswer(text=text, partials=partials)

    def extract(self, question: str) -> ExtractResult:
        """数値抽出: 表・グラフにしやすい {item, value, unit, index, source} の列を返す。"""
        partials = self._map(_EXTRACT_MAP_Q.format(question=question))
        self._emit("突き合わせ（数値抽出）", len(partials), len(partials) + 1, "JSON 生成中…")
        ctx = self._partials_block(partials)
        result = bx.llm_json(f"{_EXTRACT_REDUCE}\n\n質問: {question}\n\n調査結果:\n{ctx}")
        rows, note = [], ""
        if isinstance(result, dict):
            raw_rows = result.get("rows", [])
            note = str(result.get("note", "") or "")
        elif isinstance(result, list):
            raw_rows = result
        else:
            raw_rows = []
        for r in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(r, dict):
                continue
            try:  # value は数値に正規化（文字列 "1,234" 等にも耐える）
                v = r.get("value")
                if isinstance(v, str):
                    v = float(v.replace(",", "").strip())
                v = float(v)
            except (TypeError, ValueError):
                continue
            rows.append({
                "item": str(r.get("item", "?")),
                "value": v,
                "unit": str(r.get("unit", "") or ""),
                "index": str(r.get("index", "") or ""),
                "source": str(r.get("source", "") or ""),
            })
        self._emit("完了", 1, 1, f"{len(rows)} 件の数値を抽出")
        return ExtractResult(rows=rows, note=note, partials=partials)

    # ---- 内部: Map / Reduce ---------------------------------------------------

    def _map(self, question: str) -> list[Partial]:
        """各索引をそれぞれの方式で調べる（Map）。"""
        partials: list[Partial] = []
        total = len(self._infos) + 1  # +1 は Reduce 分
        for i, info in enumerate(self._infos):
            self._emit(f"索引を調査: {info.name}", i, total,
                       f"{info.kind} / {'、'.join(info.books[:3]) or '(文書名不明)'}")
            try:
                engine = self._engine(info)
                if info.kind == "book":
                    ans = engine.query(question)
                    hits = getattr(ans, "evidence", [])
                    path_attr = "source"  # Evidence.source = 元ファイル絶対パス
                else:
                    ans = engine.query(question, top_k=self.top_k)
                    hits = getattr(ans, "sources", [])
                    path_attr = "path"    # Source.path = 元ファイル絶対パス
                refs = [{"label": getattr(h, "title", None) or "?",
                         "page": getattr(h, "page", None),
                         "path": getattr(h, path_attr, None)} for h in hits]
                text, sources = ans.text, [str(s) for s in hits]
            except Exception as e:  # noqa: BLE001  1索引の失敗で全体を落とさない
                text, sources, refs = f"（この索引の調査に失敗: {e}）", [], []
            partials.append(Partial(index=info.name, kind=info.kind, text=text,
                                    sources=[f"[{info.name}] {s}" for s in sources],
                                    refs=refs))
        return partials

    def _reduce(self, system: str, question: str, partials: list[Partial]) -> str:
        self._emit("突き合わせ（Reduce）", len(partials), len(partials) + 1, "統合回答を生成中…")
        ctx = self._partials_block(partials)
        text = bx.llm_text(f"質問/テーマ: {question}\n\n索引ごとの調査結果:\n{ctx}",
                           system=system).strip()
        self._emit("完了", 1, 1, "")
        return text

    @staticmethod
    def _partials_block(partials: list[Partial]) -> str:
        return "\n\n".join(
            f"### 索引「{p.index}」（方式: {p.kind}）\n{p.text}"
            for p in partials
        )

    def _engine(self, info: IndexInfo):
        """索引の種類に応じたエンジンを遅延生成して使い回す。"""
        eng = self._engines.get(info.path)
        if eng is None:
            if info.kind == "book":
                from .bookrag import BookRAG

                eng = BookRAG(storage_dir=info.path)
            else:
                from .pagedrag import PagedRAG

                eng = PagedRAG(storage_dir=info.path, top_k=self.top_k)
            self._engines[info.path] = eng
        return eng

    def _emit(self, stage: str, current: int, total: int, detail: str) -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb({"stage": stage, "current": current,
                                   "total": total, "detail": detail})
            except Exception:  # noqa: BLE001  進捗表示の失敗で処理は止めない
                pass
        else:
            bx.log(f"({min(current + 1, total)}/{total}) {stage}"
                   + (f" — {detail}" if detail else ""))
