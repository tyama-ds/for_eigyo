"""Copilot Research — M365 Copilot（Researcher）を擬似GEPAで駆動するリサーチ統合アプリ。

流れ:

    調査依頼 → LocalLLM が「目次（章立て）」を設計（人手編集可）
      → 擬似GEPA で「リサーチ指示」を反復改良しながら、各章を M365 Copilot の
        リサーチエージェント経由で調査（採点→内省→進化→Pareto選択）
      → 勝ち残った指示で全章を確定リサーチ
      → LocalLLM が 1 本のレポートへ統合

擬似GEPA（Reflective Prompt Evolution + Pareto 選択）:
- 最適化対象 = 各章プロンプトへ共通で差し込む「リサーチ指示文」。
- 章（目次の各章）= タスク・インスタンス。指示を章の minibatch で評価し、
  章ごとのスコア・ベクトルを得る（多目的）。
- 内省: 弱かった章のトレース＋審査フィードバックを LLM に読ませ、指示を書き換える（変異）。
- Pareto: 章ベクトル上で非劣な候補群（フロンティア）を維持し、そこから次の親を選ぶ
  （全体最良でなくても、ある章で最良の候補は生き残る＝多様性を保つ）。

設計は llmlab Loop（loopsys.py）と同じ流儀:
- 標準ライブラリのみ（http.server）。127.0.0.1 のみ bind。接続情報はメモリのみ。
- 進捗は SSE (/api/events) でリアルタイム配信。
- LLM も M365 も未接続の「デモ実行」で、目次→GEPA→統合まで一通り体験できる。

起動::

    python -m llmlab.copilotresearch          # http://127.0.0.1:8767
    python -m llmlab.copilotresearch --port 9200

JupyterLab から::

    import llmlab
    llmlab.launch_copilot_research()
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse

from . import m365copilot

APP_NAME = "Copilot Research"
APP_VERSION = "0.7.1"  # llmlab.__version__ と合わせて更新する。/api/status と GUI ヘッダに表示される

DEFAULT_PORT = 8767
_UI_PATH = Path(__file__).parent / "copilot_ui.html"

_RUNS_MAX = 60  # レポート全文を保存するため控えめに
_store_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 永続ストア（実行履歴 = レポート付き）
# ---------------------------------------------------------------------------

def _dir() -> Path:
    from .workspace import LLMLAB_DIR

    return LLMLAB_DIR / "copilot"


def runs_all() -> list[dict]:
    from .workspace import _read_json_file

    raw = _read_json_file(_dir() / "runs.json", [])
    return raw if isinstance(raw, list) else []


def _runs_append(entry: dict) -> None:
    from .workspace import _write_json_file

    with _store_lock:
        _write_json_file(_dir() / "runs.json", ([entry] + runs_all())[:_RUNS_MAX])


# ---------------------------------------------------------------------------
# LLM ヘルパ（目次 / 審査 / 内省 / 統合）
# ---------------------------------------------------------------------------

_OUTLINE_SYSTEM = """\
あなたはリサーチの構成設計者です。調査依頼を、体系的で重複の無い「目次（章立て）」へ分解します。
出力は JSON のみ: {"title":"レポートの題名","chapters":[{"title":"章タイトル","goal":"この章で明らかにすること(一行)"}, ...]}
規則: 章は 3〜6 個。MECE を意識し、概要→各論→比較→示唆 の流れ。各章は独立に調査できる粒度にする。"""

_JUDGE_SYSTEM = """\
あなたはリサーチ品質の審査員です。ある章の調査結果を評価します。
出力は JSON のみ:
{"coverage":0..1,"specificity":0..1,"evidence":0..1,"relevance":0..1,"score":0..1,"feedback":"改善のための一行"}
基準: coverage=章ゴールの網羅 / specificity=具体・数値・事例 / evidence=出典・根拠の明示 / relevance=依頼との適合。
score は 4 項目の総合（厳しめに）。feedback は次に直すべき点を 1 つだけ。"""

_REFLECT_SYSTEM = """\
あなたは GEPA の「内省プロンプト進化器」です。各章に共通で付ける『リサーチ指示文』を、
実行トレース（調査結果の抜粋）と審査フィードバックから改良します。
出力は JSON のみ: {"instruction":"改良後の指示文（そのまま各章プロンプトへ差し込む本文）","rationale":"何を直したか一行"}
規則:
- 弱かった観点（出典不足・具体性不足・依頼との乖離 等）を潰す具体的な文言を足す。
- 全章に効く汎用の指示にする（特定の章名・固有名詞は入れない）。
- 既存の良い点は保持し、冗長化させない（本文は 8 行以内）。"""

_INTEGRATE_SYSTEM = """\
あなたはリサーチ統合ライターです。章ごとの調査結果を、1 本の一貫したレポートへ統合します。
規則:
- Markdown。冒頭に「## エグゼクティブサマリ」（3〜5 行）。以降は目次の章立てに沿う。
- 各章は調査結果を要約・再構成し、重複を除く。数値・事例は残す。
- 主張には可能な範囲で出典を残し、末尾に「## 出典一覧」をまとめる。
- 最後に「## 結論と次アクション」で依頼へ直接答える。"""


def _extract_json(text: str) -> dict:
    """LLM 出力から最初の JSON オブジェクトを頑健に取り出す。"""
    from .client import strip_think

    text = strip_think(text)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError(f"JSON が見つかりません: {text[:200]}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError(f"JSON が閉じていません: {text[start:start + 200]}")


_SEED_INSTRUCTION = (
    "この章の目的に沿って、事実に基づき体系的に調査してください。"
    "要点を箇条書きで示し、可能なら具体例を挙げてください。"
)


def propose_outline(request: str, *, demo: bool = False) -> dict:
    """調査依頼から目次（title, chapters[]）を作る。demo は LLM 不要の台本。"""
    request = request.strip()
    if demo:
        return {
            "title": f"{request[:40]} — 調査レポート",
            "chapters": [
                {"title": "背景と全体像", "goal": "テーマの定義・範囲・重要性を押さえる"},
                {"title": "現状と主要論点", "goal": "現在の状況・課題・論点を整理する"},
                {"title": "選択肢の比較", "goal": "主要な手法/選択肢をトレードオフで比較する"},
                {"title": "示唆と次アクション", "goal": "依頼への結論と推奨アクションを導く"},
            ],
        }
    from .client import complete

    raw = complete(f"調査依頼:\n{request}\n\n目次を JSON で。", system=_OUTLINE_SYSTEM, temperature=0.3)
    data = _extract_json(raw)
    chapters = []
    for c in data.get("chapters", []):
        t = str(c.get("title", "")).strip()
        if t:
            chapters.append({"title": t, "goal": str(c.get("goal", "")).strip()})
    if not chapters:
        raise ValueError("目次を生成できませんでした（章が空）")
    return {"title": str(data.get("title") or f"{request[:40]} — 調査レポート"), "chapters": chapters}


def _chapter_prompt(request: str, title: str, chapter: dict, instruction: str) -> str:
    """M365 Copilot（Researcher）へ投げる 1 章分のプロンプトを組み立てる。"""
    return (
        f"# 調査依頼\n{request}\n\n"
        f"# レポート全体の題目\n{title}\n\n"
        f"# あなたが担当する章\n{chapter['title']}\n目的: {chapter.get('goal', '')}\n\n"
        f"# リサーチ指示\n{instruction}\n\n"
        "上記の『担当する章』についてのみ、深く調査して日本語で回答してください。"
    )


_WEIGHTS = {"coverage": 0.3, "specificity": 0.3, "evidence": 0.25, "relevance": 0.15}


def _demo_judge(chapter: dict, result: m365copilot.ChapterResult) -> tuple[float, dict]:
    """デモ用の決定的採点（LLM 不要）。回答の特徴量から算出する。"""
    t = result.text or ""
    has_cite = bool(result.citations)
    has_num = bool(re.search(r"[+\-]?\d[\d,\.]*\s*(%|倍|件|億|万|人|社)", t))
    has_cmp = any(k in t for k in ("比較", "トレードオフ", "対照", "優位"))
    has_recent = any(k in t for k in ("最新", "近年", "動向", "トレンド"))
    d = {
        "coverage": round(0.5 + 0.2 * has_cmp + 0.15 * has_recent, 3),
        "specificity": round(0.35 + 0.4 * has_num + 0.15 * has_cmp, 3),
        "evidence": round(0.2 + 0.65 * has_cite, 3),
        "relevance": 0.7,
        "feedback": ("出典を明示すると良い" if not has_cite else
                     "数値で定量化すると良い" if not has_num else
                     "最新動向・比較を足すと良い" if not (has_recent and has_cmp) else "十分"),
    }
    d = {k: min(1.0, v) if isinstance(v, float) else v for k, v in d.items()}
    score = round(sum(_WEIGHTS[k] * d[k] for k in _WEIGHTS), 3)
    d["score"] = score
    return score, d


def _llm_judge(request: str, chapter: dict, result: m365copilot.ChapterResult) -> tuple[float, dict]:
    from .client import complete

    if not result.ok or not (result.text or "").strip():
        return 0.0, {"score": 0.0, "coverage": 0, "specificity": 0, "evidence": 0,
                     "relevance": 0, "feedback": result.error or "回答が空"}
    prompt = (f"調査依頼:\n{request}\n\n章:\n{chapter['title']} / 目的: {chapter.get('goal', '')}\n\n"
              f"章の調査結果:\n{result.text[:5000]}\n\n判定を JSON で。")
    try:
        d = _extract_json(complete(prompt, system=_JUDGE_SYSTEM, temperature=0.0))
    except (ValueError, json.JSONDecodeError):
        return 0.6, {"score": 0.6, "feedback": "審査JSONの解析に失敗（暫定0.6）"}
    for k in ("coverage", "specificity", "evidence", "relevance", "score"):
        try:
            d[k] = max(0.0, min(1.0, float(d.get(k, 0))))
        except (TypeError, ValueError):
            d[k] = 0.0
    if not d.get("score"):
        d["score"] = round(sum(_WEIGHTS[k] * d.get(k, 0) for k in _WEIGHTS), 3)
    d.setdefault("feedback", "")
    return d["score"], d


def _demo_reflect(instruction: str, born_iter: int, worst_fb: str) -> tuple[str, str]:
    """デモ用の決定的な指示進化（反復ごとに要求を強めていく）。"""
    additions = [
        ("結論の各要点には必ず【出典】を URL か資料名で明示してください。", "出典の明示を追加"),
        ("重要な主張は数値で定量化し、選択肢は比較（トレードオフ）で示してください。", "定量化と比較を追加"),
        ("直近1〜2年の最新動向と、依頼への実務的な示唆を必ず含めてください。", "最新動向と実務示唆を追加"),
    ]
    add, why = additions[min(born_iter, len(additions) - 1)]
    if add.split("。")[0] in instruction:  # 既に入っていれば次の要求へ
        add, why = additions[min(born_iter + 1, len(additions) - 1)]
    return (instruction.rstrip() + "\n" + add), why


def _llm_reflect(request: str, cand: "Candidate", minibatch: list[dict]) -> tuple[str, str]:
    from .client import complete

    # 弱かった章のトレースとフィードバックを最大 2 件まとめて内省させる
    worst = sorted(minibatch, key=lambda ch: cand.scores.get(ch["title"], 0))[:2]
    traces = []
    for ch in worst:
        det = cand.details.get(ch["title"], {})
        res = cand.results.get(ch["title"], {})
        traces.append(f"■章『{ch['title']}』 スコア={cand.scores.get(ch['title'], 0):.2f} "
                      f"指摘={det.get('feedback', '')}\n抜粋: {str(res.get('text', ''))[:500]}")
    prompt = (f"調査依頼:\n{request}\n\n現在のリサーチ指示:\n{cand.instruction}\n\n"
              f"弱かった章のトレースと審査フィードバック:\n" + "\n\n".join(traces) +
              "\n\n改良後の指示を JSON で。")
    try:
        d = _extract_json(complete(prompt, system=_REFLECT_SYSTEM, temperature=0.5))
        instr = str(d.get("instruction", "")).strip()
        if not instr:
            raise ValueError("empty")
        return instr, str(d.get("rationale", "")).strip() or "指示を改良"
    except (ValueError, json.JSONDecodeError):
        # 失敗時は最弱章の指摘を末尾に足すフォールバック
        fb = cand.details.get(worst[0]["title"], {}).get("feedback", "具体性を上げる") if worst else "具体性を上げる"
        return cand.instruction.rstrip() + f"\n{fb}を必ず満たしてください。", "フォールバック改良"


def integrate_report(request: str, outline: dict, results: dict, *, demo: bool = False) -> str:
    """章ごとの結果を 1 本のレポートへ統合する。"""
    chapters = outline["chapters"]
    if demo:
        lines = [f"# {outline['title']}", "", "## エグゼクティブサマリ",
                 f"本レポートは「{request}」について {len(chapters)} 章立てで調査した統合結果（デモ）。",
                 "擬似GEPA により出典・定量・比較を段階的に強化した指示で各章を調査した。", ""]
        all_cites: list[str] = []
        for i, ch in enumerate(chapters, 1):
            r = results.get(ch.get("cid", i - 1), {})
            lines += [f"## {i}. {ch['title']}", f"*目的: {ch.get('goal', '')}*", "",
                      str(r.get("text", "（結果なし）")), ""]
            all_cites += r.get("citations", [])
        if all_cites:
            lines += ["## 出典一覧"] + [f"- {c}" for c in dict.fromkeys(all_cites)] + [""]
        lines += ["## 結論と次アクション",
                  f"- 「{request}」に対し、各章の要点を統合した上記が回答。",
                  "- 次アクション: 実接続（M365 Copilot / LocalLLM）で本実行し、出典を精査する。"]
        return "\n".join(lines)

    from .client import complete

    body = []
    for i, ch in enumerate(chapters, 1):
        r = results.get(ch.get("cid", i - 1), {})
        body.append(f"## 第{i}章 {ch['title']}（目的: {ch.get('goal', '')}）\n"
                    f"{r.get('text', '（結果なし）')}\n出典候補: {', '.join(r.get('citations', []) or ['なし'])}")
    prompt = (f"調査依頼:\n{request}\n\nレポート題目: {outline['title']}\n\n"
              f"章ごとの調査結果:\n\n" + "\n\n".join(body) +
              "\n\n以上を 1 本の統合レポート（Markdown）にしてください。")
    return complete(prompt, system=_INTEGRATE_SYSTEM, temperature=0.3, max_tokens=4000)


# ---------------------------------------------------------------------------
# 擬似GEPA — 候補・Pareto・選択
# ---------------------------------------------------------------------------

class Candidate:
    """1 つのリサーチ指示候補（＝GEPAの遺伝子）。

    id は **run ごとに採番**する（並行実行しても衝突しないよう、グローバルな連番は使わない）。
    """

    def __init__(self, cid: str, instruction: str, parent: str | None, born_iter: int,
                 rationale: str = ""):
        self.id = cid
        self.instruction = instruction
        self.parent = parent
        self.born_iter = born_iter
        self.rationale = rationale
        self.scores: dict[str, float] = {}   # chapter title -> score
        self.details: dict[str, dict] = {}
        self.results: dict[str, dict] = {}    # chapter title -> ChapterResult.to_dict()
        self.agg = 0.0

    def to_dict(self, pareto_ids: set[str] | None = None) -> dict:
        return {"id": self.id, "parent": self.parent, "born_iter": self.born_iter,
                "instruction": self.instruction, "rationale": self.rationale,
                "scores": {k: round(v, 3) for k, v in self.scores.items()},
                "agg": round(self.agg, 3),
                "pareto": bool(pareto_ids and self.id in pareto_ids)}


def _dominates(a: Candidate, b: Candidate, keys: list[str]) -> bool:
    """a が b を（章ベクトル上で）支配するか: 全章 >= かつ どこかで >。"""
    ge = all(a.scores.get(k, 0) >= b.scores.get(k, 0) - 1e-9 for k in keys)
    gt = any(a.scores.get(k, 0) > b.scores.get(k, 0) + 1e-9 for k in keys)
    return ge and gt


def _pareto_front(cands: list[Candidate], keys: list[str]) -> list[Candidate]:
    return [c for c in cands if not any(_dominates(o, c, keys) for o in cands if o is not c)]


def _num_best(c: Candidate, front: list[Candidate], keys: list[str]) -> int:
    """章ごとにフロンティア内で最良（同点含む）である章の数。"""
    n = 0
    for k in keys:
        best = max(o.scores.get(k, 0) for o in front)
        if c.scores.get(k, 0) >= best - 1e-9:
            n += 1
    return n


def _select_parent(front: list[Candidate], keys: list[str], it: int) -> Candidate:
    """Pareto フロンティアから次の親を選ぶ（最多勝ち→総合→新しさ、反復でローテート）。"""
    ranked = sorted(front, key=lambda c: (_num_best(c, front, keys), c.agg, c.born_iter),
                    reverse=True)
    return ranked[it % len(ranked)]


# ---------------------------------------------------------------------------
# 実行中 Run（SSE・人手ブリッジ・キャンセル）
# ---------------------------------------------------------------------------

class _Run:
    def __init__(self, run_id: str, payload: dict):
        self.id = run_id
        self.payload = payload
        self.queue: Queue = Queue()
        self.cancelled = False
        self._human = threading.Event()
        self._resp: dict | None = None

    def emit(self, evt: dict) -> None:
        self.queue.put(evt)

    def ask_bridge(self, prompt: str, meta: dict) -> dict:
        """bridge コネクタ用: プロンプトを提示して貼り戻しを待つ。"""
        self._human.clear()
        self._resp = None
        self.emit({"type": "bridge", "prompt": prompt, "meta": meta})
        # 貼り戻しは時間がかかる想定（既定 30 分）。キャンセルは cancelled で抜ける。
        deadline = time.time() + 1800
        while time.time() < deadline:
            if self._human.wait(1.0):
                return self._resp or {"decision": "skip"}
            if self.cancelled:
                return {"decision": "skip"}
        return {"decision": "skip", "text": ""}

    def respond(self, resp: dict) -> None:
        self._resp = resp
        self._human.set()


_runs: dict[str, _Run] = {}
_runs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# オーケストレータ — 目次 → 擬似GEPA → 確定リサーチ → 統合
# ---------------------------------------------------------------------------

def _orchestrate(run: _Run) -> None:  # noqa: C901  本流は一本で読める方が保守しやすい
    p = run.payload
    demo = bool(p.get("demo"))
    emit = run.emit
    t0 = time.time()
    request = str(p.get("request", "")).strip()
    connector_kind = "demo" if demo else str(p.get("connector", "demo"))
    connector = m365copilot.make_connector(connector_kind, p.get("connector_options") or {})
    budget, mb_size = 0, 2  # 既定（数値パースは try 内。finally でも budget を参照するため先に定義）
    status, report, best = "failed", "", None
    cache: dict[tuple, m365copilot.ChapterResult] = {}
    seq = [0]  # run ローカルの候補採番（並行実行しても衝突しない）

    def new_candidate(instruction: str, parent: str | None, born_iter: int,
                      rationale: str = "") -> Candidate:
        seq[0] += 1
        return Candidate(f"c{seq[0]}", instruction, parent, born_iter, rationale)

    def stage(name: str, **kw) -> None:
        emit({"type": "stage", "stage": name, **kw})
        if demo:
            time.sleep(0.3)

    def do_research(instruction: str, ch: dict, idx: int) -> m365copilot.ChapterResult:
        # キャッシュキーは章の一意ID(cid)。タイトル重複でも取り違えない。
        key = (instruction, ch["cid"])
        if key in cache:
            return cache[key]
        prompt = _chapter_prompt(request, outline["title"], ch, instruction)
        meta = {"chapter": ch["title"], "goal": ch.get("goal", ""), "topic": request, "idx": idx}
        res = connector.research(prompt, meta=meta, emit=emit, ask_bridge=run.ask_bridge,
                                 should_cancel=lambda: run.cancelled)
        if res.ok:  # 失敗（transient なタイムアウト等）はキャッシュしない → 後で再試行できる
            cache[key] = res
        return res

    def judge(ch: dict, res: m365copilot.ChapterResult) -> tuple[float, dict]:
        return _demo_judge(ch, res) if demo else _llm_judge(request, ch, res)

    try:
        if not request:
            raise ValueError("調査依頼を入力してください")
        try:  # 数値パースは try 内で（不正でも finally が回り error/done を配信する）
            budget = max(0, min(8, int(p.get("gepa_budget", 3) or 0)))
            mb_size = max(1, min(6, int(p.get("minibatch", 2) or 2)))
        except (TypeError, ValueError):
            raise ValueError("gepa_budget / minibatch は数値で指定してください")
        if not demo and connector_kind == "graph":
            ok, msg = connector.test()
            if not ok:
                raise ValueError(f"コネクタ設定エラー: {msg}")
        stage("trigger", detail=connector_kind)

        # --- 1) 目次（人手編集済みが来ればそれを使う） ---------------------------
        stage("outline")
        if p.get("outline") and p["outline"].get("chapters"):
            outline = {"title": str(p["outline"].get("title") or f"{request[:40]} — 調査レポート"),
                       "chapters": [c for c in p["outline"]["chapters"] if c.get("title")]}
        else:
            outline = propose_outline(request, demo=demo)
        chapters = outline["chapters"]
        for i, ch in enumerate(chapters):  # 章に一意ID（タイトル重複対策の内部キー）
            ch["cid"] = i
        emit({"type": "outline", "title": outline["title"], "chapters": chapters})

        # minibatch: 代表章を均等サンプル（GEPA の探索は小バッチで回す）
        n = len(chapters)
        step = max(1, n // mb_size)
        minibatch = chapters[::step][:mb_size] or chapters[:1]
        keys = [c["cid"] for c in minibatch]
        emit({"type": "log", "text": f"目次 {n} 章 / GEPA minibatch {len(minibatch)} 章 / 予算 {budget} 反復"})

        # --- 2) 擬似GEPA: 指示を反復改良 ---------------------------------------
        pool: list[Candidate] = []

        def evaluate(cand: Candidate, it: int) -> None:
            for i, ch in enumerate(minibatch):
                if run.cancelled:
                    return
                stage("research", cand=cand.id, chapter=ch["title"])
                res = do_research(cand.instruction, ch, chapters.index(ch))
                stage("judge", cand=cand.id, chapter=ch["title"])
                s, det = judge(ch, res)
                cand.scores[ch["cid"]] = s
                cand.details[ch["cid"]] = det
                cand.results[ch["cid"]] = res.to_dict()
                emit({"type": "chapter", "phase": "gepa", "cand": cand.id, "idx": i,
                      "title": ch["title"], "connector": res.connector, "ok": res.ok,
                      "score": round(s, 3), "citations": res.citations,
                      "preview": (res.text or res.error)[:400]})
            cand.agg = round(statistics.fmean(cand.scores.values()), 3) if cand.scores else 0.0
            front_ids = {c.id for c in _pareto_front(pool + [cand], keys)}
            d = cand.to_dict(front_ids)
            # 表示用にスコアのキー（cid）を章タイトルへ戻す
            d["scores"] = {chapters[k]["title"] if isinstance(k, int) and 0 <= k < len(chapters)
                           else str(k): round(v, 3) for k, v in cand.scores.items()}
            emit({"type": "gepa_candidate", "iter": it, **d})

        stage("gepa")
        seed = new_candidate(_SEED_INSTRUCTION, parent=None, born_iter=0, rationale="初期指示（シード）")
        pool.append(seed)
        evaluate(seed, 0)

        for it in range(1, budget + 1):
            if run.cancelled:
                break
            front = _pareto_front(pool, keys)
            emit({"type": "gepa_pareto", "ids": [c.id for c in front]})
            parent = _select_parent(front, keys, it - 1)
            emit({"type": "gepa_select", "iter": it, "id": parent.id, "agg": parent.agg})
            stage("reflect", parent=parent.id, iter=it)
            if demo:
                instr, why = _demo_reflect(parent.instruction, parent.born_iter, "")
            else:
                instr, why = _llm_reflect(request, parent, minibatch)
            child = new_candidate(instr, parent=parent.id, born_iter=it, rationale=why)
            pool.append(child)
            evaluate(child, it)

        if run.cancelled:
            status = "cancelled"
            raise _Cancelled()

        front = _pareto_front(pool, keys)
        emit({"type": "gepa_pareto", "ids": [c.id for c in front]})
        best = max(pool, key=lambda c: (c.agg, c.born_iter))
        emit({"type": "gepa_best", "id": best.id, "agg": best.agg, "instruction": best.instruction})

        # --- 3) 勝ち残った指示で全章を確定リサーチ（minibatch はキャッシュ再利用） --
        stage("finalize")
        results: dict[str, dict] = {}
        for idx, ch in enumerate(chapters):
            if run.cancelled:
                status = "cancelled"
                raise _Cancelled()
            stage("research", cand=best.id, chapter=ch["title"], final=True)
            res = do_research(best.instruction, ch, idx)
            results[ch["cid"]] = res.to_dict()
            s, det = judge(ch, res)
            emit({"type": "chapter", "phase": "final", "idx": idx, "title": ch["title"],
                  "connector": res.connector, "ok": res.ok, "score": round(s, 3),
                  "citations": res.citations, "preview": (res.text or res.error)[:600]})

        # --- 4) 統合 ---------------------------------------------------------
        stage("integrate")
        report = integrate_report(request, outline, results, demo=demo)
        status = "succeeded"
        stage("done")
        emit({"type": "final", "title": outline["title"], "report": report,
              "best_instruction": best.instruction, "best_agg": best.agg,
              "candidates": len(pool)})

    except _Cancelled:
        emit({"type": "log", "text": "キャンセルされました"})
    except Exception as e:  # noqa: BLE001  失敗は UI に表示する
        emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        try:  # コネクタの後始末（selenium はここでブラウザを閉じる）
            connector.close()
        except Exception:  # noqa: BLE001
            pass
        elapsed = round(time.time() - t0, 1)
        try:
            _runs_append({
                "id": run.id, "ts": time.strftime("%Y-%m-%d %H:%M"),
                "request": request, "title": (report.splitlines()[0].lstrip("# ").strip()
                                              if report else request[:60]),
                "connector": connector_kind, "demo": demo, "status": status,
                "chapters": len((locals().get("outline") or {}).get("chapters", [])),
                "gepa": {"budget": budget, "candidates": len(locals().get("pool", []) or []),
                         "best_agg": best.agg if best else 0.0,
                         "best_instruction": best.instruction if best else ""},
                "elapsed_sec": elapsed, "report": report,
                "preview": report.replace("\n", " ")[:200],
            })
        except Exception:  # noqa: BLE001  履歴保存の失敗で全体を落とさない
            pass
        emit({"type": "status", "status": status, "elapsed_sec": elapsed})
        emit({"type": "done"})
        with _runs_lock:
            _runs.pop(run.id, None)


class _Cancelled(Exception):
    pass


def _start_run(payload: dict) -> str:
    run_id = uuid.uuid4().hex[:12]
    run = _Run(run_id, payload)
    with _runs_lock:
        _runs[run_id] = run
    threading.Thread(target=_orchestrate, args=(run,), daemon=True).start()
    return run_id


# ---------------------------------------------------------------------------
# HTTP サーバ
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = f"llmlabCopilotResearch/{APP_VERSION}"

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):  # 静かに
        pass

    # ---- GET -----------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                body = _UI_PATH.read_bytes()
            except OSError:
                self._json({"error": "copilot_ui.html が見つかりません"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/status":
            self._api_status()
        elif url.path == "/api/connectors":
            self._json({"connectors": m365copilot.connector_kinds()})
        elif url.path == "/api/runs":
            self._json({"runs": [{k: v for k, v in r.items() if k != "report"}
                                 for r in runs_all()]})
        elif url.path == "/api/run":
            rid = (parse_qs(url.query).get("id") or [""])[0]
            hit = next((r for r in runs_all() if r.get("id") == rid), None)
            self._json(hit or {"error": "not found"}, 200 if hit else 404)
        elif url.path == "/api/events":
            self._api_events((parse_qs(url.query).get("id") or [""])[0])
        else:
            self._json({"error": "not found"}, 404)

    # ---- POST ----------------------------------------------------------------

    def do_POST(self):  # noqa: N802
        route = {
            "/api/configure": self._api_configure,
            "/api/connector/test": self._api_connector_test,
            "/api/outline": self._api_outline,
            "/api/run": self._api_run,
            "/api/bridge/respond": self._api_bridge_respond,
            "/api/cancel": self._api_cancel,
        }.get(urlparse(self.path).path)
        if route:
            route()
        else:
            self._json({"error": "not found"}, 404)

    # ---- API 実装 -------------------------------------------------------------

    def _api_status(self) -> None:
        from .config import is_configured

        info = {"configured": is_configured(), "app": APP_NAME, "version": APP_VERSION}
        if is_configured():
            from .config import get_settings

            s = get_settings()
            info.update(base_url=s.base_url, model=s.model)
        self._json(info)

    def _api_configure(self) -> None:
        from .config import configure

        p = self._read_json()
        try:
            configure(
                base_url=str(p.get("base_url", "")).strip(),
                api_key=str(p.get("api_key", "")),
                model=str(p.get("model", "")).strip(),
                embed_model=str(p.get("embed_model", "")).strip() or None,
                use_proxy=bool(p.get("use_proxy", False)),
                request_timeout=float(p.get("request_timeout") or 180.0),
            )
            self._json({"ok": True})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 400)

    def _api_connector_test(self) -> None:
        p = self._read_json()
        conn = m365copilot.make_connector(str(p.get("kind", "demo")), p.get("options") or {})
        try:
            ok, msg = conn.test()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"{type(e).__name__}: {e}"
        self._json({"ok": ok, "message": msg})

    def _api_outline(self) -> None:
        """目次だけ先に生成する（人手編集 UX 用）。"""
        from .config import is_configured

        p = self._read_json()
        demo = bool(p.get("demo"))
        request = str(p.get("request", "")).strip()
        if not request:
            self._json({"ok": False, "error": "調査依頼を入力してください"}, 400)
            return
        if not demo and not is_configured():
            self._json({"ok": False, "error": "LocalLLM が未接続です（CONNECT から設定するかデモ実行を使ってください）"}, 400)
            return
        try:
            outline = propose_outline(request, demo=demo)
            self._json({"ok": True, **outline})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)

    def _api_run(self) -> None:
        from .config import is_configured

        p = self._read_json()
        if not p.get("demo") and not is_configured():
            self._json({"error": "LocalLLM が未接続です（CONNECT から設定するかデモ実行を使ってください）"}, 400)
            return
        if not str(p.get("request", "")).strip():
            self._json({"error": "調査依頼を入力してください"}, 400)
            return
        for k in ("gepa_budget", "minibatch"):  # 数値以外は早めに弾く
            if k in p and p[k] not in (None, ""):
                try:
                    int(p[k])
                except (TypeError, ValueError):
                    self._json({"error": f"{k} は数値で指定してください"}, 400)
                    return
        self._json({"run_id": _start_run(p)})

    def _api_bridge_respond(self) -> None:
        p = self._read_json()
        run = _runs.get(str(p.get("run_id", "")))
        if run is None:
            self._json({"ok": False, "error": "実行が見つかりません（終了済み？）"}, 404)
            return
        run.respond({"decision": str(p.get("decision", "submit")),
                     "text": str(p.get("text", ""))})
        self._json({"ok": True})

    def _api_cancel(self) -> None:
        p = self._read_json()
        run = _runs.get(str(p.get("run_id", "")))
        if run is None:
            self._json({"ok": False, "error": "実行が見つかりません"}, 404)
            return
        run.cancelled = True
        run.respond({"decision": "skip", "text": ""})
        self._json({"ok": True})

    def _api_events(self, run_id: str) -> None:
        run = _runs.get(run_id)
        if run is None:
            self._json({"error": "unknown run"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    evt = run.queue.get(timeout=30)
                except Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                if evt.get("type") == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# 起動
# ---------------------------------------------------------------------------

def serve(port: int = DEFAULT_PORT, *, open_browser: bool = False):
    """Copilot Research サーバを起動する（ブロッキング）。Ctrl+C で終了。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"{APP_NAME} v{APP_VERSION}: {url}  （Ctrl+C で終了）")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def launch_copilot_research(port: int = DEFAULT_PORT) -> str:
    """ノートブックからバックグラウンド起動して URL を返す。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"{APP_NAME} v{APP_VERSION} を起動しました: {url}\n"
          "（LocalLLM は右上 CONNECT から。M365 Copilot はコネクタ選択。"
          "未接続でも『デモ実行』で 目次→擬似GEPA→統合 を体験できます）")
    return url


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Copilot Research（M365 Copilot × 擬似GEPA リサーチ）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="起動時にブラウザを開く")
    args = ap.parse_args(argv)
    serve(port=args.port, open_browser=args.open)


if __name__ == "__main__":
    main()
