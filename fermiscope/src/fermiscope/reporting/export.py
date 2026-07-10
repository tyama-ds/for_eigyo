"""エクスポート: JSON / CSV / スタンドアロンHTML / Markdown。"""

from __future__ import annotations

import csv
import html
import io
import json
from typing import Any

from fermiscope.domain.models import EstimateProject
from fermiscope.reporting.builder import build_report


def export_json(project: EstimateProject) -> str:
    return json.dumps(build_report(project), ensure_ascii=False, indent=2, default=str)


def export_csv(project: EstimateProject) -> dict[str, str]:
    """CSVは2ファイル(パラメータ・証拠)を返す。"""
    report = build_report(project)

    params_buf = io.StringIO()
    pw = csv.writer(params_buf)
    pw.writerow(
        [
            "id",
            "name",
            "unit",
            "central",
            "low",
            "high",
            "distribution",
            "value_basis",
            "confidence",
            "geography",
            "period",
            "critique_count",
            "decomposition_status",
            "assumptions",
        ]
    )
    for p in report["parameters"]:
        pw.writerow(
            [
                p["id"],
                p["name"],
                p["unit"],
                p["central"],
                p["low"],
                p["high"],
                p["distribution"],
                p["value_basis"],
                p["confidence"],
                p["geography"],
                p["period"],
                p["critique_count"],
                p["decomposition_status"],
                " / ".join(p["assumptions"]),
            ]
        )

    ev_buf = io.StringIO()
    ew = csv.writer(ev_buf)
    ew.writerow(
        [
            "id",
            "parameter_id",
            "url",
            "title",
            "source_class",
            "evidence_score",
            "publication_date",
            "retrieval_date",
            "time_period",
            "geography",
            "extracted_value",
            "unit",
            "excerpt",
            "accepted",
            "ai_assisted",
        ]
    )
    for e in report["evidence"]:
        ew.writerow(
            [
                e["id"],
                e["parameter_id"],
                e["url"],
                e["title"],
                e["source_class"],
                e["evidence_score"],
                e["publication_date"],
                e["retrieval_date"],
                e["time_period"],
                e["geography"],
                e["extracted_value"],
                e["unit"],
                e["excerpt"],
                e["accepted"],
                e["ai_assisted"],
            ]
        )
    return {"parameters.csv": params_buf.getvalue(), "evidence.csv": ev_buf.getvalue()}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def export_markdown(project: EstimateProject) -> str:
    r = build_report(project)
    c = r["conclusion"]
    lines: list[str] = []
    lines.append(f"# {r['project']['name'] or r['question']['original_question']}")
    lines.append("")
    lines.append(f"**問い**: {r['question']['original_question']}")
    lines.append("")
    lines.append(
        f"## 結論\n\n**中心推定値: {c['central_display']} {r['question']['target_unit']}**"
        f"(P10–P90: {c['range_display']}、信頼度 {c['confidence'] if c['confidence'] is not None else '—'})"
    )
    lines.append("")
    lines.append("## シナリオ")
    lines.append("")
    lines.append("| シナリオ | 値 | 説明 |")
    lines.append("|---|---|---|")
    for s in r["scenarios"]:
        lines.append(f"| {s['name']} | {s['value_display']} | {s['description']} |")
    lines.append("")
    lines.append("## 推定式")
    for m in r["models"]:
        if m["role"] in ("primary", "check"):
            role = "主モデル" if m["role"] == "primary" else "検算モデル"
            lines.append(f"- **{role}** {m['name']}: `{m['expression_raw']}`")
    lines.append("")
    lines.append("## パラメータ")
    lines.append("")
    lines.append("| ID | 名称 | 中心値 | 範囲 | 単位 | 由来 | 信頼度 |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in r["parameters"]:
        rng = (
            f"{p['low']:g} – {p['high']:g}" if p["low"] is not None and p["high"] is not None else "—"
        )
        central = f"{p['central']:g}" if p["central"] is not None else "未解決"
        lines.append(
            f"| {p['id']} | {p['name']} | {central} | {rng} | {p['unit']} | {p['value_basis']} | "
            f"{p['confidence'] if p['confidence'] is not None else '—'} |"
        )
    lines.append("")
    if r["irreducible_assumptions"]:
        lines.append("## 分解不能な仮定")
        for i in r["irreducible_assumptions"]:
            lines.append(f"- **{i['parameter_id']}**: {i['reason']}")
        lines.append("")
    if r["contradictions"]:
        lines.append("## 証拠間の矛盾")
        for con in r["contradictions"]:
            lines.append(f"- {con['parameter_id']}: {con['note']}")
            for v in con["analysis"].values():
                lines.append(f"  - {v}")
        lines.append("")
    if r["validation"]:
        v = r["validation"]
        lines.append("## 検算モデルとの比較")
        lines.append(
            f"- 中心値の比: {v['central_ratio']} / 区間重なり: {v['interval_overlap']} / 判定: {v['agreement']}"
        )
        for w in v["warnings"]:
            lines.append(f"- ⚠ {w}")
        lines.append("")
    lines.append("## 出典一覧")
    lines.append("")
    lines.append("| クラス | スコア | タイトル | URL | 発行日 | 取得日 |")
    lines.append("|---|---|---|---|---|---|")
    for e in r["evidence"]:
        lines.append(
            f"| {e['source_class']} | {e['evidence_score']} | {e['title']} | {e['url']} | "
            f"{e['publication_date'] or '—'} | {(e['retrieval_date'] or '')[:10]} |"
        )
    lines.append("")
    run = r["run"] or {}
    lines.append(
        f"---\n再現情報: シード {r['simulation']['config']['seed']} / "
        f"反復 {r['simulation']['config']['iterations']} / "
        f"アプリ {r['project']['app_version']} / 設定ハッシュ {r['project']['config_hash']} / "
        f"検索 {run.get('searches_executed', 0)} 回"
    )
    return "\n".join(lines)


def export_html(project: EstimateProject, app_name: str = "FermiScope") -> str:
    """スタンドアロンHTMLレポート(外部依存なし・全データ埋め込み)。"""
    r = build_report(project)
    c = r["conclusion"]

    scenario_rows = "".join(
        f"<tr><td>{_esc(s['name'])}</td><td class='num'>{_esc(s['value_display'])}</td>"
        f"<td>{_esc(s['description'])}</td></tr>"
        for s in r["scenarios"]
    )
    param_rows = ""
    for p in r["parameters"]:
        rng = (
            f"{p['low']:g} – {p['high']:g}"
            if p["low"] is not None and p["high"] is not None
            else "—"
        )
        param_rows += (
            f"<tr><td>{_esc(p['name'])}</td><td class='num'>{_esc(p['central_display'])}</td>"
            f"<td class='num'>{_esc(rng)}</td><td>{_esc(p['unit'])}</td>"
            f"<td>{_esc(p['value_basis'])}</td><td>{_esc(p['confidence'])}</td>"
            f"<td>{_esc(p['critique_count'])}</td></tr>"
        )

    evidence_rows = ""
    for e in r["evidence"]:
        evidence_rows += (
            f"<tr><td>{_esc(e['source_class'])}</td><td class='num'>{_esc(e['evidence_score'])}</td>"
            f"<td>{_esc(e['title'])}<br><small>{_esc(e['url'])}</small></td>"
            f"<td>{_esc(e['publication_date'] or '—')}</td>"
            f"<td>{_esc((e['retrieval_date'] or '')[:10])}</td>"
            f"<td>{_esc(e['excerpt'][:120])}</td></tr>"
        )

    caveats = "".join(f"<li>{_esc(x)}</li>" for x in c["key_caveats"])
    formulas = "".join(
        f"<p><b>{'主モデル' if m['role'] == 'primary' else '検算モデル'}</b> "
        f"{_esc(m['name'])}: <code>{_esc(m['expression_raw'])}</code></p>"
        for m in r["models"]
        if m["role"] in ("primary", "check")
    )
    validation_html = ""
    if r["validation"]:
        v = r["validation"]
        warns = "".join(f"<li>{_esc(w)}</li>" for w in v["warnings"])
        validation_html = (
            f"<h2>検算モデルとの比較</h2><p>中心値の比: {_esc(v['central_ratio'])} / "
            f"区間重なり: {_esc(v['interval_overlap'])} / 判定: {_esc(v['agreement'])}</p>"
            f"<ul>{warns}</ul>"
        )

    embedded = json.dumps(r, ensure_ascii=False, default=str).replace("</", "<\\/")
    run = r["run"] or {}
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(app_name)} レポート — {_esc(r["question"]["original_question"])}</title>
<style>
body{{font-family:'Hiragino Sans','Noto Sans JP',sans-serif;margin:2rem auto;max-width:960px;
padding:0 1rem;color:#1a202c;line-height:1.7}}
h1{{font-size:1.4rem;border-bottom:3px solid #2b6cb0;padding-bottom:.5rem}}
h2{{font-size:1.1rem;margin-top:2rem;color:#2b6cb0}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}
th,td{{border:1px solid #cbd5e0;padding:.4rem .6rem;text-align:left;vertical-align:top}}
th{{background:#ebf4ff}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.conclusion{{background:#ebf8ff;border:1px solid #90cdf4;border-radius:8px;padding:1rem 1.5rem}}
.conclusion .big{{font-size:1.8rem;font-weight:700}}
small{{color:#4a5568}}
code{{background:#edf2f7;padding:.1rem .3rem;border-radius:4px}}
footer{{margin-top:3rem;font-size:.8rem;color:#4a5568;border-top:1px solid #cbd5e0;padding-top:1rem}}
</style>
</head>
<body>
<h1>{_esc(app_name)} 推定レポート</h1>
<p><b>問い:</b> {_esc(r["question"]["original_question"])}</p>
<div class="conclusion">
  <div class="big">{_esc(c["central_display"])} {_esc(r["question"]["target_unit"])}</div>
  <div>妥当範囲(P10–P90): {_esc(c["range_display"])} / 信頼度: {_esc(c["confidence"])}</div>
</div>
<h2>主な注意点</h2>
<ul>{caveats or "<li>特記事項なし</li>"}</ul>
<h2>シナリオ</h2>
<table><tr><th>シナリオ</th><th>値</th><th>説明</th></tr>{scenario_rows}</table>
<h2>推定式</h2>
{formulas}
<h2>パラメータ</h2>
<table><tr><th>名称</th><th>中心値</th><th>範囲(P10–P90)</th><th>単位</th><th>由来</th>
<th>信頼度</th><th>批判数</th></tr>{param_rows}</table>
{validation_html}
<h2>出典一覧</h2>
<table><tr><th>クラス</th><th>スコア</th><th>タイトル / URL</th><th>発行日</th><th>取得日</th>
<th>根拠箇所</th></tr>{evidence_rows}</table>
<footer>
生成: {_esc(r["project"]["updated_at"])} / アプリ {_esc(app_name)} v{_esc(r["project"]["app_version"])}
/ シード {_esc(r["simulation"]["config"]["seed"])} / 設定ハッシュ {_esc(r["project"]["config_hash"])}
/ 検索 {_esc(run.get("searches_executed", 0))} 回
</footer>
<script type="application/json" id="report-data">{embedded}</script>
</body>
</html>"""
