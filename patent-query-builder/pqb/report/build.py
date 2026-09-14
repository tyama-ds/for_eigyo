"""根拠レポート（Markdown／HTML）。企画書 §4 出力 3・§9.1 pqb.report。

- 3 案（DB 別レンダリング済み文字列と DSL 参照）
- 検索観点表（観点×語×分類、採否と理由）
- 各語・各分類の採用理由、由来文献、RSJ 統計、フリップ率
- 評価ログ（反復ごとの件数・既知文献再現率・プール再現率・上位K適合率・推定再現率）
- 版履歴・判断ログ・注記（U 基準の推定、局所照合と DB 照合の差）
"""
from __future__ import annotations

import html
import re

VARIANT_JA = {"broad": "広め", "standard": "標準", "narrow": "狭め"}
STATUS_JA = {"adopted": "採用", "rejected": "棄却", "candidate": "保留"}


def _f(v, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _table(header: list[str], rows: list[list]) -> str:
    def cell(x) -> str:
        return str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(c) for c in r) + " |")
    return "\n".join(out)


def build_markdown(bundle: dict) -> str:
    case = bundle["case"]
    cfg = bundle.get("config") or {}
    axes = bundle["axes"]
    cands = bundle["candidates"]
    its = bundle["iterations"]
    last = bundle.get("latest_metrics") or {}
    reason_codes = cfg.get("reason_codes") or {}
    lines: list[str] = []
    lines.append(f"# 検索式 根拠レポート — {case.get('name') or case['case_id']}")
    lines.append("")
    lines.append(_table(["項目", "内容"], [
        ["案件ID", case["case_id"]], ["調査種別", bundle.get("purpose", {}).get("label", case.get("purpose"))],
        ["状態", case.get("status")], ["反復", case.get("iteration")], ["既知文献", ", ".join(case.get("seeds") or []) or "なし"],
        ["期間", f"{case.get('date_from') or '—'} 〜 {case.get('date_to') or '—'}"], ["国", ",".join(case.get("countries") or [])],
        ["作成", case.get("created_at")], ["LLM 経路", cfg.get("llm_mode")], ["分類表辞書", "縮退モード（観測コードのみ）" if bundle.get("dictionary", {}).get("degraded") else "公式データあり"],
    ]))
    lines.append("")
    lines.append("## 1. 入力（技術説明）")
    lines.append("")
    lines.append("> " + (case.get("input_text") or "").replace("\n", "\n> "))
    lines.append("")

    # 3 案
    lines.append("## 2. 検索式（3 案）")
    lines.append("")
    final_it = case.get("iteration")
    latest = [q for q in bundle["queries"] if q["iteration"] == final_it] or bundle["queries"][-3:]
    variants_m = last.get("variants") or {}
    rec = (last.get("pareto") or {}).get("recommended")
    for v in ("broad", "standard", "narrow"):
        q = next((x for x in latest if x["variant"] == v), None)
        if not q:
            continue
        m = variants_m.get(v) or {}
        mark = "（推奨）" if rec == v else ""
        lines.append(f"### {VARIANT_JA[v]}案{mark} — `{q['query_id']}`")
        lines.append("")
        if m:
            lines.append(_table(["件数", "既知文献再現率", "プール再現率", "上位K適合率", "複雑さ", "取得元"],
                                [[m.get("hit_count"), _f(m.get("recall_seed")), _f(m.get("recall_pool")), _f(m.get("p_at_k")), m.get("complexity"), m.get("source")]]))
            lines.append("")
        for r in q["renderings"]:
            tag = f"{r['dialect']}" + (f" (part {r['part_no']})" if r["part_no"] > 1 or any(x["part_no"] > 1 for x in q["renderings"] if x["dialect"] == r["dialect"]) else "")
            lines.append(f"- **{tag}** {r['chars']} 文字、往復検証 {'OK' if r['roundtrip_ok'] else 'NG'}")
            lines.append("")
            lines.append("```")
            lines.append(r["text"])
            lines.append("```")
        s = q.get("summary") or {}
        for b in s.get("blocks", []):
            lines.append(f"- Block {b['axis_id']} {b['axis_name']}（{'必須' if b['required'] else '補助'}、{b['join']}、{','.join(b['fields'])}）: "
                         f"語 {len(b['terms'])}、コード {len(b['codes'])}")
        lines.append("")

    # 観点表
    lines.append("## 3. 検索観点表")
    lines.append("")
    lines.append(_table(["観点ID", "観点名", "種別", "定義", "根拠（入力文）", "由来"],
                        [[a["axis_id"], a["name"], "必須" if a["kind"] == "required" else "補助", a.get("definition", ""), a.get("evidence", ""), a.get("origin", "")] for a in axes]))
    lines.append("")
    for a in axes:
        terms = [c for c in cands if c["kind"] == "term" and c["axis_id"] == a["axis_id"]]
        codes = [c for c in cands if c["kind"] == "code" and c["axis_id"] == a["axis_id"]]
        lines.append(f"### 観点 {a['axis_id']}: {a['name']}")
        lines.append("")
        if terms:
            lines.append("**語**")
            lines.append("")
            lines.append(_table(["語", "表記種別", "由来", "由来文献", "r/n", "R/N", "RSJ w", "OW", "標本 w", "フリップ率", "採否", "理由"],
                                [[c["value"], c.get("variant_kind", ""), c.get("origin", ""), c.get("origin_doc", ""),
                                  f"{_f(c.get('r'))}/{_f(c.get('n'))}" if c.get("n") is not None else "—",
                                  f"{_f(c.get('R'))}/{_f(c.get('N'))}" if c.get("N") is not None else "—",
                                  _f(c.get("rsj_w")), _f(c.get("offer_w")), _f(c.get("w_sample")), _f(c.get("flip_rate")),
                                  STATUS_JA.get(c["status"], c["status"]) + ("（要確認）" if c.get("needs_review") else ""),
                                  reason_codes.get(c.get("reason_code") or "", c.get("reason_code") or "") + (f" / {c['note']}" if c.get("note") else "")]
                                 for c in sorted(terms, key=lambda x: (x["status"] != "adopted", -(x.get("offer_w") or 0)))]))
            lines.append("")
        if codes:
            lines.append("**分類**")
            lines.append("")
            lines.append(_table(["体系", "コード", "粒度", "タイトル（辞書）", "辞書照合", "由来", "r/n", "RSJ w", "OW", "フリップ率", "採否", "理由"],
                                [[c["scheme"], c["value"], c.get("level", ""), c.get("title", ""), {True: "済", False: "未照合", None: ""}.get(c.get("dict_known"), ""),
                                  c.get("origin", ""), f"{_f(c.get('r'))}/{_f(c.get('n'))}" if c.get("n") is not None else "—",
                                  _f(c.get("rsj_w")), _f(c.get("offer_w")), _f(c.get("flip_rate")),
                                  STATUS_JA.get(c["status"], c["status"]) + ("（要確認）" if c.get("needs_review") else ""),
                                  reason_codes.get(c.get("reason_code") or "", c.get("reason_code") or "") + (f" / {c['note']}" if c.get("note") else "")]
                                 for c in sorted(codes, key=lambda x: (x["status"] != "adopted", -(x.get("offer_w") or 0)))]))
            lines.append("")

    # 採用理由の文章（P6）
    expl = (case.get("settings") or {}).get("explanations") or []
    if expl:
        lines.append("## 4. 採用理由（文章）")
        lines.append("")
        lines.append("数値はシステム側で計算した値をそのまま用いている（LLM に生成させていない）。")
        lines.append("")
        for e in expl:
            lines.append(f"- **{e.get('axis_id', '')} {e.get('value', '')}** — {e.get('text', '')}")
        lines.append("")

    # 評価ログ
    lines.append("## 5. 評価ログ（反復ごと）")
    lines.append("")
    rows = []
    for it in its:
        m = it["metrics"]
        for v, vm in (m.get("variants") or {}).items():
            est = (m.get("estimates") or {}).get(v) or {}
            rows.append([it["iteration"], VARIANT_JA.get(v, v), vm.get("hit_count"), _f(vm.get("recall_seed")), _f(vm.get("recall_pool")),
                         _f(vm.get("p_at_k")), f"{_f(est.get('recall_hat'))} [{_f(est.get('ci_low'))}, {_f(est.get('ci_high'))}]" + ("（低信頼）" if est.get("low_confidence") else "") if est else "—",
                         "○" if vm.get("in_range") else "×", vm.get("source")])
    lines.append(_table(["反復", "案", "件数", "既知再現率", "プール再現率", "P@K", "推定再現率 [95%CI]", "レンジ内", "取得元"], rows))
    lines.append("")
    if last:
        j = last.get("judged") or {}
        lines.append(f"- 採点済み N={j.get('N')}（適合 R={j.get('R')}）、プール {j.get('pool')} 件、無作為標本 m={j.get('sample_m')}（適合 s={j.get('sample_s')}）、要確認 {j.get('needs_review')} 件")
        stop = last.get("stop") or {}
        lines.append(f"- 停止判定: {'停止条件を満たす' if stop.get('should_stop') else '継続'}（{', '.join(stop.get('reasons') or []) or '—'}）。目標レンジ {stop.get('population_range')}")
        if last.get("retention") is not None:
            lines.append(f"- 初回上位適合文献の保持率: {_f(last['retention'])}" + ("（警告: ドリフトの可能性）" if last.get("retention_warning") else ""))
        if last.get("llm_agreement_with_stats") is not None:
            lines.append(f"- LLM 変換提案と統計候補の一致率: {_f(last['llm_agreement_with_stats'])}")
        lines.append("")
        st = last.get("stats") or {}
        if st.get("terms") or st.get("codes"):
            lines.append("### RSJ 統計（上位）")
            lines.append("")
            lines.append(st.get("note", ""))
            lines.append("")
            lines.append(_table(["種別", "素性", "r", "n", "w", "OW", "標本 w", "要確認"],
                                [["語", s["feature"], s["r"], s["n"], _f(s["w"]), _f(s["ow"]), _f(s.get("w_sample")), "要" if s.get("needs_review") else ""] for s in st.get("terms", [])[:10]]
                                + [[f"{s['scheme']}", f"{s['feature']} {s.get('title', '')}", s["r"], s["n"], _f(s["w"]), _f(s["ow"]), _f(s.get("w_sample")), "要" if s.get("needs_review") else ""] for s in st.get("codes", [])[:10]]))
            lines.append("")
        if last.get("dnf"):
            lines.append("### 決定木の適合経路（候補生成器）")
            lines.append("")
            for p in last["dnf"][:6]:
                lits = " ∧ ".join(("" if l["present"] else "¬") + l["feature"] for l in p["literals"])
                lines.append(f"- {lits}（{p['n']} 件中 {p['n_pos']} 件適合）")
            lines.append("")

    # 変換
    tfs = bundle.get("transforms") or []
    if tfs:
        lines.append("## 6. 変換候補と採否")
        lines.append("")
        lines.append(_table(["反復", "操作", "対象", "発生源", "方向", "予測件数", "予測プール再現率", "予測P@K", "局所評価", "退行", "採否", "理由"],
                            [[t["iteration"], t["op"], _target(t.get("target") or {}), t.get("source"), t.get("direction"), t.get("pred_hits"),
                              _f(t.get("pred_recall_pool")), _f(t.get("pred_p_at_k")), "済" if t.get("local_eval") else "", "退行" if t.get("regression") else "",
                              STATUS_JA.get(t["status"], t["status"]), t.get("reason", "")] for t in tfs]))
        lines.append("")

    # 版履歴・判断ログ
    lines.append("## 7. 版履歴")
    lines.append("")
    lines.append(_table(["反復", "案", "query_id", "親", "作成", "作成者"],
                        [[q["iteration"], VARIANT_JA.get(q["variant"], q["variant"]), q["query_id"], q.get("parent_query_id") or "—", q.get("created_at"), ""] for q in bundle["queries"]]))
    lines.append("")
    lines.append("## 8. 判断ログ")
    lines.append("")
    lines.append(_table(["日時", "反復", "ゲート", "決定者", "操作", "内容"],
                        [[d["created_at"], d["iteration"], d["gate"], d["actor"], d["action"], _short(d.get("payload"))] for d in bundle.get("decisions") or []]))
    lines.append("")
    lines.append("## 9. 注記（限界）")
    lines.append("")
    lines.append("- RSJ 統計・推定再現率は母集団 U（広め案の結果）の内側で計算した値であり、U の外側の取り逃しは測れない。広め案の再現率は既知文献と引用拡張で担保する。")
    lines.append("- 局所照合は名称・要約・請求の範囲の部分一致とコードの階層一致で行っており、DB 側の照合（全文フィールド、シソーラス展開等）と差がある。")
    lines.append("- J-PlatPat 論理式の文法は暫定（企画書 Q3）。式は人が DB に貼って実行する。分類表辞書が縮退モードの場合、タイトル未照合のコードは要確認。")
    if (case.get("settings") or {}).get("sample"):
        lines.append("- 本案件は同梱の合成データ（実データではない）で作成した。")
    lines.append("")
    return "\n".join(lines)


def _target(t: dict) -> str:
    parts = []
    for k in ("axis_id", "text", "scheme", "code", "new_code", "join", "fields", "other_axis_id"):
        if k in t and t[k] not in (None, "", []):
            parts.append(f"{k}={t[k]}")
    return " ".join(parts)


def _short(payload) -> str:
    if not payload:
        return ""
    if isinstance(payload, dict):
        keys = [k for k in payload if k not in ("axes", "decisions", "extra")]
        s = ", ".join(f"{k}={payload[k]}" for k in keys)
        if "axes" in payload:
            s = f"axes={len(payload['axes'])} " + s
        return s[:200]
    return str(payload)[:200]


# ------------------------------------------------------------------ Markdown → HTML（最小）

def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def markdown_to_html(md: str, title: str = "根拠レポート") -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_list = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[-|\s]+\|$", lines[i + 1]):
            header = [c.strip() for c in line.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip().replace("\\|", "|") for c in lines[i].strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in header) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")
            continue
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
            i += 1
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    if in_list:
        out.append("</ul>")
    css = ("body{font-family:system-ui,-apple-system,'Segoe UI','Hiragino Sans','Yu Gothic',sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;"
           "color:#1c2333;line-height:1.55}table{border-collapse:collapse;font-size:.85rem;margin:.5rem 0 1rem;display:block;overflow-x:auto}"
           "th,td{border:1px solid #d7dbe7;padding:.3rem .5rem;vertical-align:top}th{background:#eef1fa}pre{background:#f4f6fd;padding:.7rem;"
           "border-radius:8px;white-space:pre-wrap;word-break:break-all}blockquote{border-left:4px solid #8b5cf6;margin:0;padding:.2rem 1rem;color:#444}"
           "h1{border-bottom:2px solid #8b5cf6;padding-bottom:.3rem}h2{margin-top:2rem;border-bottom:1px solid #d7dbe7}code{background:#f4f6fd;padding:0 .2rem}")
    return (f"<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'><title>{html.escape(title)}</title><style>{css}</style></head>"
            f"<body>{''.join(out)}</body></html>")


def build_html(bundle: dict) -> str:
    md = build_markdown(bundle)
    return markdown_to_html(md, title=f"根拠レポート {bundle['case']['case_id']}")
