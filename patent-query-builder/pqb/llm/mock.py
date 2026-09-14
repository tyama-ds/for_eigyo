"""オフライン用のモック LLM。企画書 §14.2「config.offline=true で fixtures を返す」の実装。

LLM 無しでもパイプライン全体が動くように、各プロンプトの出力をヒューリスティックに作る。
出力は必ず対応するスキーマ（pqb/llm/schemas）に適合する。案件固有の知識は持たず、
入力（技術説明・既知文献・統計）だけから決定的に生成する。人が G1〜G4 で修正する前提。
"""
from __future__ import annotations

import random
import re
from collections import Counter

from ..stats.tokenize import candidates_from_text, dedupe_by_postings, load_stopwords
from ..util import norm_text

# 採点の揺らぎ（テスト用。0 なら決定的）
JITTER = 0.0

_KANJI_KATA = re.compile(r"^[一-龥々〆ヶァ-ヴー]+[ぁ-ん]{0,2}$")
_PROCESS_CUES = ("焼", "冷", "加熱", "処理", "工程", "圧延", "成形", "溶接", "塗", "めっき", "析出", "添加",
                 "制御", "接合", "切断", "研磨", "洗浄", "乾燥", "硬化", "熱")
_USE_CUES = ("用", "部材", "部品", "車", "向け", "製品", "構造", "機器", "装置向け")
_EFFECT_CUES = ("向上", "抑制", "防止", "低減", "改善", "耐", "性", "確保", "安定")

# デモ領域の小さな同義語表（辞書が育つまでの初期値）
SYNONYMS = {
    "高強度鋼板": ["高張力鋼板", "ハイテン", "高強度鋼", "high strength steel sheet"],
    "高張力鋼板": ["高強度鋼板", "ハイテン", "high tensile steel"],
    "焼入れ": ["焼き入れ", "急冷", "クエンチ", "quenching"],
    "焼戻し": ["焼き戻し", "テンパー", "tempering"],
    "自動車部材": ["自動車部品", "車体部品", "自動車用部材", "automotive part"],
    "耐食性": ["耐腐食性", "防食性", "corrosion resistance"],
    "めっき": ["メッキ", "鍍金", "plating"],
    "鋼板": ["鋼帯", "薄鋼板", "steel sheet"],
    "ホットスタンプ": ["熱間プレス", "ホットプレス", "hot stamping"],
    "水素脆化": ["水素脆性", "遅れ破壊", "hydrogen embrittlement"],
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。\n]", text or "") if s.strip()]


def _ranked_tokens(sents: list[str], stop: set[str]) -> tuple[list[str], dict[str, set[int]]]:
    postings: dict[str, set[int]] = {}
    for i, s in enumerate(sents):
        for tok in candidates_from_text(s, stop):
            postings.setdefault(tok, set()).add(i)
    postings = dedupe_by_postings(postings)
    ranked = sorted(postings, key=lambda t: (-len(postings[t]), -len(t), t))
    return ranked, postings


def _related(a: str, b: str) -> bool:
    return a in b or b in a


def _overlap(a: str, b: str) -> int:
    """2 文字以上の共通部分文字列の最大長。"""
    best = 0
    for i in range(len(a)):
        for j in range(i + 2, len(a) + 1):
            if a[i:j] in b:
                best = max(best, j - i)
    return best


# ---------------------------------------------------------------- P1
def p1_structure(inputs: dict) -> dict:
    text = inputs.get("input_text", "")
    sents = _sentences(text)
    stop = load_stopwords()
    ranked, postings = _ranked_tokens(sents, stop)
    good = [t for t in ranked if _KANJI_KATA.match(t) and len(t) >= 2]
    first = set(candidates_from_text(sents[0], stop)) if sents else set()

    def pick(pred, exclude: list[str]) -> str | None:
        for t in good:
            if any(_related(t, e) for e in exclude):
                continue
            if pred(t):
                return t
        return None

    axes = []
    obj = pick(lambda t: t in first, []) or (good[0] if good else None)
    if obj:
        # 最頻語を含む、より長い（具体的な）語が第 1 文にあればそれを対象物にする（鋼板 → 高強度鋼板）
        longer = [t for t in first if obj in t and t != obj and _KANJI_KATA.match(t)
                  and not any(c in t for c in _PROCESS_CUES)]
        if longer:
            obj = sorted(longer, key=lambda t: (-len(t), t))[0]
    means = pick(lambda t: any(c in t for c in _PROCESS_CUES) or t.endswith("れ"), [obj] if obj else [])
    if means is None:
        means = pick(lambda t: True, [obj] if obj else [])
    use = pick(lambda t: any(c in t for c in _USE_CUES) and not any(c in t for c in _PROCESS_CUES),
               [x for x in (obj, means) if x])
    effect = pick(lambda t: any(c in t for c in _EFFECT_CUES), [x for x in (obj, means, use) if x])

    def evidence(tok: str) -> str:
        for i in sorted(postings.get(tok, [])):
            return sents[i]
        for s in sents:
            if tok in s:
                return s
        return sents[0] if sents else ""

    spec = [("A", "対象物", "required", "structure", obj),
            ("B", "手段", "required", "structure", means),
            ("C", "用途", "auxiliary", "use", use),
            ("D", "効果", "auxiliary", "effect", effect)]
    for axis_id, name, kind, cat, tok in spec:
        if not tok:
            continue
        axes.append({"axis_id": axis_id, "name": f"{name}（{tok}）", "kind": kind, "category": cat,
                     "definition": f"「{tok}」に関する観点。", "evidence": evidence(tok), "terms": [tok]})
    if not axes:
        axes.append({"axis_id": "A", "name": "対象物", "kind": "required", "category": "structure",
                     "definition": "入力文の主題。", "evidence": text[:80], "terms": []})
    return {"axes": axes, "notes": "モック LLM による簡易分解。人が G1 で確認・修正する。", "low_confidence": True}


# ---------------------------------------------------------------- P2
def p2_expand(inputs: dict) -> dict:
    axes = inputs.get("axes", [])
    terms_by_axis: dict[str, list[str]] = inputs.get("terms_by_axis", {})
    docs = inputs.get("relevant_docs", [])
    hints: dict[str, list[str]] = inputs.get("synonyms_hint", {}) or {}
    stop = load_stopwords()
    out = []
    seen: set[tuple[str, str]] = set()

    def add(axis_id, text, kind, origin, conf):
        key = (axis_id, norm_text(text))
        if key in seen or not text:
            return
        seen.add(key)
        out.append({"axis_id": axis_id, "text": text, "variant_kind": kind, "origin": origin, "confidence": conf})

    for ax in axes:
        aid = ax["axis_id"]
        base_terms = list(terms_by_axis.get(aid, [])) or list(ax.get("terms", []))
        for t in base_terms:
            for s in SYNONYMS.get(t, []):
                kind = "english" if s.isascii() else ("variant" if _overlap(s, t) >= 2 else "synonym")
                add(aid, s, kind, "input", 0.7)
            for s in hints.get(t, []):
                add(aid, s, "synonym", "dict", 0.8)
            # 送り仮名の表記ゆれ（焼入れ ⇄ 焼き入れ）
            m = re.fullmatch(r"([一-龥])([一-龥])(れ|し|け|り)", t)
            if m:
                add(aid, f"{m.group(1)}き{m.group(2)}{m.group(3)}", "variant", "input", 0.5)
        # 文献からの抽出
        per_axis = 0
        for d in docs:
            if per_axis >= 8:
                break
            toks = candidates_from_text(f"{d.get('title', '')}\n{d.get('abstract', '')}", stop)
            added_here: list[str] = []
            for tok in sorted(toks, key=lambda x: (-len(x), x)):
                if not _KANJI_KATA.match(tok) or len(tok) < 3:
                    continue
                if any(tok == b for b in base_terms) or any(tok in a for a in added_here):
                    continue
                if any(_overlap(tok, b) >= 2 and tok not in b for b in base_terms):
                    add(aid, tok, "extracted", f"doc:{d.get('doc_id', '')}", 0.5)
                    added_here.append(tok)
                    per_axis += 1
                    if per_axis >= 8:
                        break
    return {"terms": out, "low_confidence": True}


# ---------------------------------------------------------------- P3
def p3_classify(inputs: dict) -> dict:
    axes = inputs.get("axes", [])
    terms_by_axis: dict[str, list[str]] = inputs.get("terms_by_axis", {})
    counts: dict[str, list[dict]] = inputs.get("seed_code_counts", {}) or {}
    known: list[dict] = inputs.get("known_codes", []) or []
    required = [a for a in axes if a.get("kind") == "required"] or axes
    default_axis = required[0]["axis_id"] if required else "A"
    total = int(inputs.get("seed_count") or 0)

    def axis_for(title: str) -> str:
        best, best_len = default_axis, 0
        for a in axes:
            for t in terms_by_axis.get(a["axis_id"], []) + list(a.get("terms", [])):
                ov = _overlap(t, title or "")
                if ov > best_len and ov >= 2:
                    best, best_len = a["axis_id"], ov
        return best

    out = []
    for scheme, items in counts.items():
        for it in sorted(items, key=lambda x: -x.get("count", 0))[:3]:
            conf = (it.get("count", 0) / total) if total else 0.5
            out.append({"axis_id": axis_for(it.get("title", "")), "scheme": scheme, "code": it["code"],
                        "reason": f"既知文献 {total} 件中 {it.get('count', 0)} 件に付与", "confidence": round(min(1.0, conf), 2)})
    if not out:
        for k in known:
            title = k.get("title", "")
            if any(_overlap(t, title) >= 2 for a in axes for t in terms_by_axis.get(a["axis_id"], [])):
                out.append({"axis_id": axis_for(title), "scheme": k["scheme"], "code": k["code"],
                            "reason": f"分類表タイトル「{title}」が観点の語と一致", "confidence": 0.4})
            if len(out) >= 6:
                break
    return {"codes": out, "low_confidence": True}


# ---------------------------------------------------------------- P4
def p4_judge(inputs: dict, sample_no: int = 1) -> dict:
    axes = inputs.get("axes", [])
    doc = inputs.get("doc", {})
    ti, ab, cl = norm_text(doc.get("title", "")), norm_text(doc.get("abstract", "")), norm_text(doc.get("claims", ""))
    per_axis: dict[str, int] = {}
    reasons = []
    for a in axes:
        terms = [norm_text(t) for t in a.get("terms", []) if t]
        score = 0
        hit = ""
        for t in terms:
            if not t:
                continue
            if t in ti:
                score, hit = 3, t
                break
            if t in ab or t in cl:
                score, hit = max(score, 2), t
            elif len(t) >= 3 and any(t[i:i + 2] in ti + ab for i in range(len(t) - 1)):
                score, hit = max(score, 1), t
        per_axis[a["axis_id"]] = score
        if hit:
            reasons.append(f"{a['axis_id']}:「{hit}」{'名称' if score == 3 else '要約/請求'}に出現")
    required = [a["axis_id"] for a in axes if a.get("kind") == "required"] or list(per_axis)
    overall = min((per_axis[r] for r in required), default=0)
    if JITTER > 0 and sample_no > 1:
        rng = random.Random(f"{doc.get('doc_id', '')}:{sample_no}")
        if rng.random() < JITTER:
            overall = max(0, min(3, overall + rng.choice([-1, 1])))
    rationale = "、".join(reasons) if reasons else "観点の語が名称・要約に見つからない"
    return {"per_axis": per_axis, "overall": overall, "rationale": rationale,
            "low_confidence": not bool(ab or cl)}


# ---------------------------------------------------------------- P5
def p5_transform(inputs: dict) -> dict:
    axes = inputs.get("axes", [])
    stats = inputs.get("stats", {}) or {}
    dsl = inputs.get("dsl_summary", {}) or {}
    in_terms = {norm_text(t) for b in dsl.get("blocks", []) for t in b.get("terms", [])}
    in_codes = {(c.get("scheme"), c.get("code")) for b in dsl.get("blocks", []) for c in b.get("codes", [])}
    block_axes = {b.get("axis_id") for b in dsl.get("blocks", [])}
    out = []

    def axis_for(text: str) -> str:
        best, best_len = None, 0
        for b in dsl.get("blocks", []):
            for t in b.get("terms", []):
                ov = _overlap(text, t)
                if ov > best_len:
                    best, best_len = b.get("axis_id"), ov
        return best or (dsl.get("blocks", [{}])[0].get("axis_id", "A") if dsl.get("blocks") else "A")

    fb = inputs.get("feedback") or {}
    failed_ops = {tuple(step.split(":", 1)) for f in (fb.get("failures") or []) for step in (f.get("steps") or [])}
    for t in stats.get("terms", [])[:6]:
        if norm_text(t["feature"]) in in_terms:
            continue
        out.append({"op": "ADD_TERM", "target": {"axis_id": axis_for(t["feature"]), "text": t["feature"]},
                    "reason": f"適合側に偏る語（r={t['r']}/R={t['R']}, w={t['w']}）", "expected_effect": "widen"})
    for t in stats.get("negative_terms", [])[:6]:
        if norm_text(t["feature"]) in in_terms and not any(op == "DROP_TERM" and t["feature"] in rest for op, rest in failed_ops):
            out.append({"op": "DROP_TERM", "target": {"axis_id": axis_for(t["feature"]), "text": t["feature"]},
                        "reason": f"非適合側に偏る語（n={t['n']}, w={t['w']}）", "expected_effect": "narrow"})
    for c in stats.get("codes", [])[:4]:
        if (c["scheme"], c["feature"]) in in_codes:
            continue
        out.append({"op": "ADD_CODE", "target": {"axis_id": axis_for(c.get("title", "") or ""), "scheme": c["scheme"], "code": c["feature"]},
                    "reason": f"適合側に偏る分類（r={c['r']}/R={c['R']}, OW={c['ow']}）", "expected_effect": "widen"})
    for a in axes:
        if a.get("kind") == "auxiliary" and a["axis_id"] not in block_axes:
            out.append({"op": "ADD_AXIS", "target": {"axis_id": a["axis_id"]},
                        "reason": "補助観点を AND 軸に追加して狭める", "expected_effect": "narrow"})
    return {"transforms": out[:10], "low_confidence": True}


# ---------------------------------------------------------------- P6
def p6_report(inputs: dict) -> dict:
    cands = inputs.get("candidates", [])
    out = []
    for c in cands:
        if c.get("kind") == "term":
            text = f"「{c.get('value')}」は {c.get('origin', '不明')} 由来の語。"
        else:
            text = f"分類 {c.get('scheme')} {c.get('value')}（{c.get('title') or 'タイトル未照合'}）は {c.get('origin', '不明')} 由来。"
        if c.get("N"):
            text += f" 採点済み {c['N']} 件のうち適合 {c.get('R')} 件中 {c.get('r')} 件に出現（RSJ w={c.get('rsj_w')}）。"
        if c.get("flip_rate") is not None:
            text += f" フリップ率 {c['flip_rate']}。"
        if c.get("reason_code"):
            text += f" 判断理由コード {c['reason_code']}。"
        out.append({"kind": c.get("kind", "term"), "axis_id": c.get("axis_id", ""), "value": str(c.get("value", "")), "text": text})
    return {"explanations": out, "summary": "統計値はシステム側で計算した値をそのまま記載している。", "low_confidence": False}


def generate(prompt_id: str, inputs: dict, sample_no: int = 1) -> dict:
    if prompt_id == "P1":
        return p1_structure(inputs)
    if prompt_id == "P2":
        return p2_expand(inputs)
    if prompt_id == "P3":
        return p3_classify(inputs)
    if prompt_id == "P4":
        return p4_judge(inputs, sample_no)
    if prompt_id == "P5":
        return p5_transform(inputs)
    if prompt_id == "P6":
        return p6_report(inputs)
    raise KeyError(prompt_id)
