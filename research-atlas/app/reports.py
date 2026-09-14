from __future__ import annotations

import csv
import html
import io
import json


def safe_cell(value) -> str:
    text = "" if value is None else str(value)
    # Prevent spreadsheet formula execution when opening exported user-supplied labels.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def citation_total(papers: list[dict]):
    values = [p["citations"] for p in papers if p.get("citations") is not None]
    return sum(values) if values else None


def topic_csv(result: dict) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    monthly = result.get("frontiers", {}).get("monthly", {})
    monthly_topics = {row["topic_id"]: row for row in monthly.get("topics", [])} if monthly.get("available") else {}
    writer.writerow(["Topic", "Papers", "Cumulative citations (known only)", "Growth %", "Exploratory score", "Status", "Keywords", "Forecast year", "Forecast papers", "Scenario lower", "Scenario upper", "Backtest MAE", "Naive baseline MAE", "Papers with known citation count", "Synthetic demo", "Providers", "Sampled corpus (not population growth)", "Citation sources", "Monthly anchor", "Comparison months", "Recent window papers", "Previous window papers", "Monthly growth % (null if previous zero)", "Share change pp", "Monthly signal", "Topic model", "Embedding model", "Map representation", "Unclassified group"])
    for topic in result["topics"]:
        forecast = topic.get("forecast", [])
        last = forecast[-1] if forecast else {}
        members = [p for p in result["papers"] if p["topic_id"] == topic["id"]]
        known = sum(p.get("citations") is not None for p in members)
        signal = monthly_topics.get(topic["id"], {})
        writer.writerow([safe_cell(topic["label"]), topic["count"], citation_total(members), topic["growth_pct"], topic["score"], topic["status"], safe_cell("; ".join(topic["keywords"])),
            last.get("year"), last.get("value"), last.get("lower"), last.get("upper"), topic["backtest"].get("mae"), topic["backtest"].get("baseline_mae"), known, bool(result["meta"].get("is_demo")), safe_cell("; ".join(result["meta"].get("providers", []))), bool(result["meta"].get("sampled")), safe_cell("; ".join(result["meta"].get("citation_sources", []))), monthly.get("anchor_month", ""), monthly.get("window_months", ""), signal.get("recent_count"), signal.get("baseline_count"), signal.get("growth_pct"), signal.get("share_change_pp"), signal.get("status", "unavailable"), safe_cell(result["meta"].get("topic_model", "kmeans")), safe_cell(result["meta"].get("embedding_model", "")), safe_cell(result["meta"].get("map_representation", "")), bool(topic.get("is_outlier"))])
    return "\ufeff" + stream.getvalue()


def papers_csv(papers: list[dict]) -> str:
    stream = io.StringIO(newline="")
    years = sorted({y for p in papers for y in p.get("citation_history", {})}, key=int)
    writer = csv.writer(stream)
    writer.writerow(["EID", "Title", "Year", "Authors", "Author(s) ID", "Abstract", "Author Keywords", "Cited by", "DOI", "Source title", "Data provenance", "Publication date", *[f"Citations {y}" for y in years]])
    for p in papers:
        writer.writerow([safe_cell(p["id"]), safe_cell(p["title"]), p["year"], safe_cell("; ".join(a["name"] for a in p["authors"])),
            safe_cell("; ".join(a["id"] for a in p["authors"])), safe_cell(p["abstract"]), safe_cell("; ".join(p["keywords"])), p["citations"],
            safe_cell(p["doi"]), safe_cell(p["source"]), safe_cell(p.get("provenance", "")), safe_cell(p.get("publication_date", "")), *[p["citation_history"].get(y, "") for y in years]])
    return "\ufeff" + stream.getvalue()


def frontier_html(result: dict) -> str:
    frontier = result.get("frontiers")
    if not frontier:
        return ""
    esc = lambda value: html.escape(str(value))
    monthly, sparse = frontier.get("monthly", {}), frontier.get("sparse", {})
    body = "<h2>月次の研究活動</h2>"
    body += f"<p>基準月: {esc(monthly.get('anchor_month', '—'))} / 比較窓: {esc(monthly.get('window_months', '—'))}か月 / 月が分かる論文: {esc(monthly.get('date_coverage', '—'))}%</p>"
    if not monthly.get("available"):
        body += f"<p class='note'>{esc(monthly.get('reason_label', '日付・比較期間の情報が不足しているため月次比較はできません。'))}</p>"
    else:
        recent = " / ".join(monthly.get("recent_months", []))
        baseline = " / ".join(monthly.get("baseline_months", []))
        body += f"<p>直近: {esc(recent)}<br>比較対象: {esc(baseline)}</p><table><thead><tr><th>テーマ</th><th>直近件数</th><th>比較件数</th><th>増減</th><th>構成比の変化</th><th>判定</th></tr></thead><tbody>"
        names = {"new": "新規観測候補", "rising": "増加候補", "declining": "減少", "stable": "横ばい", "insufficient": "件数不足"}
        for row in monthly.get("topics", []):
            growth = f"{row['growth_pct']:+.1f}%" if row.get("growth_pct") is not None else "比較期0件・率は未定義"
            share = f"{row['share_change_pp']:+.1f}ポイント" if row.get("share_change_pp") is not None else "—"
            body += f"<tr><td>{esc(row['label'])}</td><td>{row['recent_count']}</td><td>{row['baseline_count']}</td><td>{esc(growth)}</td><td>{esc(share)}</td><td>{esc(names.get(row['status'], row['status']))}</td></tr>"
        body += "</tbody></table>"
    body += "<ul>" + "".join(f"<li>{esc(w)}</li>" for w in monthly.get("warnings", [])) + "</ul>"
    body += "<h2>少ない組み合わせの候補</h2><p>取り込み集合内の語句の共起です。地図の空白、研究の未開拓、有望性や因果関係を認定するものではありません。</p>"
    if sparse.get("candidates"):
        body += "<table><thead><tr><th>組み合わせ</th><th>Aの文献</th><th>Bの文献</th><th>共起文献</th><th>独立仮定の参考件数</th></tr></thead><tbody>"
        for row in sparse["candidates"]:
            body += f"<tr><td>{esc(row['term_a'])} × {esc(row['term_b'])}</td><td>{row['count_a']}</td><td>{row['count_b']}</td><td>{row['observed']}</td><td>{row['expected']:.2f}</td></tr>"
        body += "</tbody></table>"
    else:
        body += "<p>設定した支持件数・共起条件を満たす候補はありません。</p>"
    return body + "<ul>" + "".join(f"<li>{esc(w)}</li>" for w in sparse.get("warnings", [])) + "</ul>"


def report_html(result: dict) -> str:
    esc = lambda v: html.escape(str(v))
    meta = result["meta"]
    rows = ""
    for t in result["topics"]:
        total = citation_total([p for p in result["papers"] if p["topic_id"] == t["id"]])
        growth = f"{t['growth_pct']:+.1f}%" if t.get("growth_pct") is not None else "比較不可"
        rows += f"<tr><td>{esc(t['label'])}</td><td>{t['count']}</td><td>{total if total is not None else '未取得'}</td><td>{esc(growth)}</td><td>{t['score']:.1f}</td><td>{esc(t['status'])}</td></tr>"
    trajectory = ""
    colors = ["#007f82", "#7950c2", "#b36c1b", "#3673c2", "#ce556a", "#467c44"]
    years = meta["years"]
    end = max((f["year"] for t in result["topics"] for f in t["forecast"]), default=years[-1])
    upper = max((f["upper"] for t in result["topics"] for f in t["forecast"]), default=1)
    top = max(upper, max((s["count"] for t in result["topics"] for s in t["series"]), default=1), 1)
    x = lambda y: 45 + (y-years[0])/max(1,end-years[0])*700
    y = lambda n: 250 - n/top*210
    legend = []
    for i,t in enumerate(result["topics"]):
        color = colors[i % len(colors)]
        actual = " ".join(f"{x(s['year']):.1f},{y(s['count']):.1f}" for s in t["series"])
        future = " ".join(f"{x(f['year']):.1f},{y(f['value']):.1f}" for f in t["forecast"])
        bridge = actual.split()[-1] if actual else ""
        trajectory += f'<polyline points="{actual}" fill="none" stroke="{color}" stroke-width="2"/><polyline points="{bridge} {future}" fill="none" stroke="{color}" stroke-width="2" stroke-dasharray="5 5"/>'
        legend.append(f'<span style="color:{color}">● {esc(t["label"])}</span>')
    axis = "".join(f'<text x="{x(year):.1f}" y="275" font-size="12" text-anchor="middle">{year}</text>' for year in range(years[0], end+1))
    warnings = "".join(f"<li>{esc(s)}</li>" for s in meta.get("warnings", []))
    methodology = "".join(f"<li>{esc(s)}</li>" for s in result.get("methodology", []))
    forecasts = ""
    for t in result["topics"]:
        if t.get("is_outlier"):
            continue
        values = "; ".join(f"{f['year']}年 {f['value']:.1f}件 [{f['lower']:.1f}, {f['upper']:.1f}]" for f in t["forecast"])
        bt = t["backtest"]
        forecasts += f"<p><b>{esc(t['label'])}</b> — {esc(values)}<br><small>検証 MAE: {esc(bt.get('mae'))} / 前年維持 MAE: {esc(bt.get('baseline_mae'))} / 検証点: {bt.get('folds',0)}</small></p>"
    citations = ""
    for r in result["timeline"]:
        total = citation_total([p for p in result["papers"] if p["year"] == r["year"]])
        citations += f"<tr><td>{r['year']}</td><td>{r['papers']}</td><td>{total if total is not None else '未取得'}</td><td>{esc(r.get('annual_citations') if r.get('annual_citations') is not None else '欠測')}</td><td>{esc(r.get('annual_coverage') if r.get('annual_coverage') is not None else '—')}%</td></tr>"
    evidence = "".join(f"<li>{esc(p['title'])} ({p['year']}) — {esc(p['id'])}</li>" for p in result["papers"][:100])
    demo = "合成・テストデータを含む分析 — 実際の研究動向の判断には使用できません" if meta.get("is_demo") else "取り込みデータの分析"
    return f'''<!doctype html><html lang="ja"><meta charset="utf-8"><title>Research Atlas 分析レポート</title>
<style>body{{font:14px/1.8 system-ui,sans-serif;color:#173039;margin:40px auto;max-width:1000px;padding:0 24px}}h1{{font-size:32px;letter-spacing:-1px}}h2{{margin-top:36px;border-bottom:1px solid #ccd8db;padding-bottom:8px}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #e0e7e9}}th{{background:#edf5f5}}small{{color:#567}}.eyebrow{{color:#007f82;letter-spacing:3px}}.note{{background:#edf5f5;padding:16px;border-left:3px solid #00888b}}svg{{width:100%;height:auto}}.legend{{display:flex;gap:12px;flex-wrap:wrap;font-size:12px}}@media print{{body{{margin:0;max-width:none;font-size:11px}}h2{{break-after:avoid}}tr,svg{{break-inside:avoid}}}}</style>
<div class="eyebrow">RESEARCH ATLAS / INTELLIGENCE REPORT</div><h1>研究の現在地と、次の兆し。</h1>
<p>{esc(result.get('dataset_name',''))} · {meta['start_year']}–{meta['end_year']} · {result['summary']['papers']:,} 論文</p><p class="note">{demo}<br>文章表現: {esc(meta.get('embedding_model',meta.get('embedding')))}<br>テーマ抽出: {esc(meta.get('topic_model','kmeans'))} / マップ表現: {esc(meta.get('map_representation',meta.get('embedding')))}<br>未分類: {meta.get('unclassified_papers',0)} 論文（テーマの注目度・予測対象から除外）<br>作成: {esc(result.get('created_at',''))}</p>
<h2>技術テーマ</h2><table><thead><tr><th>テーマ</th><th>論文</th><th>累積引用</th><th>成長率</th><th>探索スコア</th><th>状態</th></tr></thead><tbody>{rows}</tbody></table>
{frontier_html(result)}
<h2>論文件数の推移と探索的予測</h2><p>実線＝観測、破線＝外挿。帯の数値は探索範囲で、校正された信頼区間ではありません。</p>
<svg viewBox="0 0 790 290" role="img" aria-label="テーマ別年間論文件数"><line x1="45" x2="745" y1="250" y2="250" stroke="#aab"/>{trajectory}{axis}</svg><div class="legend">{' '.join(legend)}</div>{forecasts}
<h2>出版年と引用受領年</h2><p>累積引用は出版年コホート別の現在値です。年別受領引用は履歴が観測された対象論文分のみを集計します。</p><table><thead><tr><th>年</th><th>出版論文</th><th>出版年別累積引用</th><th>その年の受領引用</th><th>履歴カバー率</th></tr></thead><tbody>{citations}</tbody></table>
<h2>方法と解釈上の限界</h2><ul>{warnings}{methodology}</ul>
<h2>根拠論文（先頭100件まで）</h2><p>全件・マップ・ネットワークはJSONエクスポートに含まれます。</p><ol>{evidence}</ol></html>'''
