"""CSV and Japanese PDF rendering of one saved assessment; never invokes an LLM."""
from __future__ import annotations

from html import escape
from io import BytesIO
import json
import os
from pathlib import Path
import threading

from .field_exports import _csv

FONT_LOCK = threading.Lock()


def _numeric_warnings(value):
    validation = value.get("validation", {}) if isinstance(value, dict) else {}
    warnings = validation.get("warnings", []) if isinstance(validation, dict) else []
    return [warning for warning in warnings if isinstance(warning, dict) and warning.get("code") == "numeric_mismatch"]


def _has_numeric_warning(value):
    if not isinstance(value, dict):
        return False
    validation = value.get("validation", {})
    return bool(_numeric_warnings(value)
                or isinstance(validation, dict) and validation.get("status") == "warning"
                or value.get("verification") == "quote_checked_numeric_warning")


def _warning_tokens(warning):
    tokens = [str(value) for value in warning.get("unmatched_numbers", [])]
    for quantity in warning.get("unmatched_quantities", []):
        if isinstance(quantity, dict):
            tokens.append(" ".join(str(quantity.get(key, "")).strip() for key in ("value", "unit")).strip())
    return list(dict.fromkeys(token for token in tokens if token))


def _warning_location(location):
    parts = str(location).strip("/").split("/")
    if parts == ["headline"]:
        return "評論の見出し"
    if len(parts) >= 2 and parts[1].isdigit():
        index = str(int(parts[1]) + 1)
        if parts[0] == "sections":
            return "第" + index + "節の" + ("見出し" if parts[-1] == "title" else "本文")
        if parts[0] == "caveats":
            return "注意事項 " + index
        if parts[0] in {"facts", "content_facts"}:
            return "根拠の要約 " + index
    return ""


def _candidate_has_numeric_warning(candidate):
    narrative = candidate.get("narrative") or {}
    return (_has_numeric_warning(narrative)
            or any(_has_numeric_warning(section) for section in narrative.get("sections", []))
            or any(_has_numeric_warning(fact) for fact in (candidate.get("content_facts") or candidate.get("evidence", []))))


def assessment_csv(assessment: dict) -> str:
    rows = []
    def visit(path, value):
        if isinstance(value, dict):
            if not value:
                rows.append([path, "object", "{}"])
            for key, item in value.items():
                if key.startswith("_retrieval"):
                    continue
                visit(path + "/" + str(key).replace("~", "~0").replace("/", "~1"), item)
        elif isinstance(value, list):
            if not value:
                rows.append([path, "array", "[]"])
            for index, item in enumerate(value):
                visit(path + "/" + str(index), item)
        else:
            kind = "null" if value is None else "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string"
            rows.append([path, kind, "" if value is None else json.dumps(value) if isinstance(value, bool) else value])
    visit("", assessment)
    return _csv(["Assessment ID", "Revision", "JSON Pointer", "Value type", "Value"],
                [[assessment.get("id", ""), assessment.get("revision", 1), *row] for row in rows])


def _font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    with FONT_LOCK:
        if "AtlasJP" in pdfmetrics.getRegisteredFontNames():
            return "AtlasJP"
        choices = [os.getenv("ATLAS_PDF_FONT", ""), "C:/Windows/Fonts/meiryo.ttc",
                   "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
                   "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"]
        for name in choices:
            if name and Path(name).is_file():
                try:
                    pdfmetrics.registerFont(TTFont("AtlasJP", name, subfontIndex=0))
                    return "AtlasJP"
                except Exception:
                    continue
        raise ValueError("日本語PDF用のTrueTypeフォントを ATLAS_PDF_FONT に指定してください。")


def assessment_pdf(assessment: dict, candidate_id: str | None = None) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
        from reportlab.graphics.shapes import Drawing, Line, Rect, String, Polygon
    except ImportError:
        raise ValueError("PDF出力には reportlab が必要です。requirements.txt をインストールしてください。") from None
    font = _font()
    navy, teal = colors.HexColor("#122438"), colors.HexColor("#087F86")
    muted = colors.HexColor("#546779")
    width = A4[0] - 84
    normal = ParagraphStyle("body", fontName=font, fontSize=9, leading=15, textColor=navy, wordWrap="CJK", spaceAfter=8)
    small = ParagraphStyle("small", parent=normal, fontSize=8, leading=12, textColor=muted, spaceAfter=5)
    heading = ParagraphStyle("heading", parent=normal, fontSize=13, leading=20, textColor=teal, spaceBefore=8, spaceAfter=8, keepWithNext=True)
    title = ParagraphStyle("title", parent=normal, fontSize=20, leading=29, spaceAfter=15)
    amber = colors.HexColor("#8A4B05")
    warning_text = ParagraphStyle("warning", parent=small, textColor=amber, spaceAfter=6)
    def clean(text):
        return "".join(ch for ch in str(text) if ch in "\n\t" or ord(ch) >= 32)
    def p(text, style=normal):
        return Paragraph(escape(clean(text)).replace("\n", "<br/>"), style)
    def compact(value):
        if value is None: return "未判定"
        if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False)
        return str(value)
    def warning_block(value, scope="", explanation="", include_details=True):
        icon = Drawing(16, 16)
        icon.add(Polygon([8, 15, 0, 1, 16, 1], fillColor=colors.HexColor("#F5B82E"), strokeColor=amber, strokeWidth=.6))
        icon.add(String(8, 3, "!", fontName="Helvetica-Bold", fontSize=10, textAnchor="middle", fillColor=navy))
        label = "数値照合に失敗・要確認" + (" / " + scope if scope else "")
        banner = Table([[icon, p(label, warning_text)]], colWidths=[25, width - 25], hAlign="LEFT")
        banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF1CA")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        banner.keepWithNext = True
        blocks = [Spacer(1, 5), banner]
        if explanation:
            blocks.append(p(explanation, warning_text))
        if include_details:
            for warning in _numeric_warnings(value):
                message = warning.get("message") or "根拠または集計指標に対応しない数値が含まれています。"
                tokens = _warning_tokens(warning)
                location = _warning_location(warning.get("location", ""))
                detail = str(message) + (" 照合箇所：" + location if location else "")
                if tokens:
                    detail += " / 要確認の数値：" + "、".join(tokens)
                blocks.append(p(detail, warning_text))
        if len(blocks) == 2:
            blocks.append(p("本文は生成時の内容を保持しています。原文の数値・単位と集計指標を確認してください。", warning_text))
        return blocks
    def grid(headers, rows, fractions):
        header_style = ParagraphStyle("th", parent=small, textColor=colors.white)
        data = [[p(h, header_style) for h in headers]] + [[p(compact(c), small) for c in row] for row in rows]
        table = Table(data, colWidths=[width*f for f in fractions], repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), navy), ("VALIGN", (0,0),(-1,-1),"TOP"),
            ("ROWBACKGROUNDS", (0,1),(-1,-1),[colors.white,colors.HexColor("#EFF5F7")]),
            ("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
        return table
    def annual_chart(candidate):
        series = candidate.get("growth", {}).get("series", [])
        data = [(str(s.get("year", "")), s.get("count", s.get("papers", 0)) or 0) for s in series]
        if not data:
            return p("年次データがありません。", small)
        d = Drawing(width, 124)
        d.add(String(4,110,"収録論文の年次件数（将来予測ではありません）",fontName=font,fontSize=8,fillColor=muted))
        max_count = max([v for _,v in data]+[1])
        bw = (width-38)/max(1,len(data))
        d.add(Line(28,24,width,24,strokeColor=colors.HexColor("#B9CBD4")))
        for i,(year,count) in enumerate(data):
            x=30+i*bw
            height=count/max_count*60
            d.add(Rect(x+4,24,max(2,bw-12),height,fillColor=teal,strokeColor=None))
            d.add(String(x+bw/2,28+height,str(count),fontName=font,fontSize=7,textAnchor="middle",fillColor=navy))
            d.add(String(x+bw/2,10,year,fontName=font,fontSize=7,textAnchor="middle",fillColor=muted))
        return d
    candidates = [c for c in assessment.get("candidates", []) if not candidate_id or c["id"] == candidate_id]
    papers = {v["id"]: v for v in assessment.get("papers", [])}
    story = [p("RESEARCH ATLAS", heading), p("有望領域の探索・推薦レポート", title),
             p(f"データ：{assessment.get('dataset_name') or assessment.get('meta',{}).get('dataset_name') or assessment.get('dataset_id','')}"),
             p(f"評価ID：{assessment.get('id','')}  /  版：{assessment.get('revision',1)}",small),
             p(f"保存日時：{assessment.get('created_at','')}  /  元分析：{assessment.get('result_id','')}",small),
             p("研究の伸びと実用化の証拠を分けて読みます。根拠への言及は、実用化達成・再現性・因果関係を保証しません。反復探索は検索を改善する処理であり、予測精度の検証とは異なります。")]
    for warning in assessment.get("warnings", []):
        story.append(p(warning,small))
    warned_candidates = [candidate for candidate in candidates if _candidate_has_numeric_warning(candidate)]
    if warned_candidates:
        story += warning_block({}, "このレポートのLLM出力", explanation=
            "数値照合に失敗した評論または要約を含みます。本文は修正せず掲載し、該当箇所に警告と照合できなかった数値を示します。"
            "対象領域：" + "、".join(candidate.get("label", candidate["id"]) for candidate in warned_candidates), include_details=False)
    summary = []
    for c in candidates:
        g, r = c.get("growth",{}), c.get("readiness",{})
        growth = f"{g.get('growth_pct'):+.1f}%" if g.get("available") and isinstance(g.get("growth_pct"),(int,float)) else "未判定"
        coverage = r.get("evidence_coverage_score")
        summary.append([c.get("label",c["id"]),growth,"未判定" if coverage is None else f"{coverage}%",r.get("stage_label","未判定")])
    if summary:
        story += [p("候補の比較",heading), grid(["候補", "収録内の増減", "根拠言及充足度", "実用化段階"],summary,[.43,.17,.22,.18])]
    else:
        story.append(p("分類済みの推薦候補がありません。分析条件や入力文献を確認してください。"))
    story += [Spacer(1,12),p("成熟度・成功確率は未評価です。根拠言及充足度は項目の記載状況を示します。",small)]
    for c in candidates:
        story += [PageBreak(),p(c.get("label",c["id"]),title)]
        g,r=c.get("growth",{}),c.get("readiness",{})
        story += [p("研究動向と実用化の証拠",heading),p(g.get("reason", "")),annual_chart(c),
                  p(g.get("forecast_reason") or "内容に基づく将来予測は未検証のため数値を表示しません。",small)]
        dims = [[v.get("label",v.get("id","")),"言及あり・意味未検証" if v.get("status")=="mentioned" else "不明", v.get("paper_count",0)] for v in r.get("dimensions",[])]
        if dims: story += [grid(["証拠項目","状態","言及論文数"],dims,[.34,.46,.20]),Spacer(1,10)]
        narrative=c.get("narrative")
        if narrative:
            story += [p(narrative.get("headline","LLM評論"),heading),p(f"生成方式：{narrative.get('mode')} / モデル：{narrative.get('model')} / 数値ハッシュ：{narrative.get('numeric_hash','')[:16]}",small)]
            if _has_numeric_warning(narrative):
                story += warning_block(narrative, "LLM評論", explanation="評論は生成時の内容を保持しています。以下の警告は数値の照合結果であり、研究内容全体の真偽を判定したものではありません。")
            for section in narrative.get("sections",[]):
                story.append(p(section.get("title","評論"),heading))
                if _has_numeric_warning(section):
                    story += warning_block(section, "この節")
                story += [p(section.get("text","")),p("根拠論文："+", ".join(section.get("evidence_ids",[])),small)]
            for caveat in narrative.get("caveats",[]): story.append(p(caveat,small))
        else:
            story += [p("集計に基づく論点整理（LLM未使用）",heading)]
            comment=c.get("commentary",{})
            for key,label in [("summary","概況"),("support","支持の確認"),("counter","反証の確認"),("next_steps","次の研究"),("limitations","制約")]:
                value=comment.get(key)
                if value:
                    story.append(p(label+"："+("\n".join(map(str,value)) if isinstance(value,list) else str(value))))
        if c.get("llm_error"): story.append(p("今回のLLM生成エラー："+c["llm_error"],small))
        facts=c.get("content_facts") or c.get("evidence",[])
        displayed_facts = facts[:12] + [fact for fact in facts[12:] if _has_numeric_warning(fact)]
        evidence_heading = ("原文の根拠（先頭12件と数値要確認の根拠・全件はCSV/JSON）"
                            if len(displayed_facts) > 12 else "原文の根拠（最大12件掲載・全件はCSV/JSON）")
        story.append(p(evidence_heading,heading))
        for fact in displayed_facts:
            paper=papers.get(fact.get("paper_id"),{})
            text=fact.get("quote",fact.get("text",""))
            if fact.get("prefix_omitted"):
                text = "…" + text
            if fact.get("suffix_omitted"):
                text += "…"
            fact_heading = p(f"{paper.get('title',fact.get('paper_id',''))} / {paper.get('year','')} / {fact.get('paper_id','')}",small)
            if _has_numeric_warning(fact):
                story.append(fact_heading)
                story += warning_block(fact, "この根拠の要約")
                if fact.get("statement"):
                    story.append(p("要約：" + fact["statement"], small))
                story += [p(text,small), p("検証状態：数値照合に失敗・要確認", warning_text)]
            else:
                items = [fact_heading]
                if fact.get("statement"):
                    items.append(p("要約：" + fact["statement"], small))
                items += [p(text,small),p("検証状態："+fact.get("verification","unknown"),small)]
                story.append(KeepTogether(items))
        if not facts: story.append(p("利用できる実抄録の根拠が不足しています。",small))
        story.append(p("関連文献（上位12件）",heading))
        recommendations=c.get("recommendations",[])[:12]
        for row in recommendations:
            paper=papers.get(row.get("paper_id"),{})
            link=paper.get("doi") or paper.get("external_url") or row.get("paper_id","")
            story.append(KeepTogether([
                p(f"{paper.get('title',row.get('paper_id',''))} ({paper.get('year','')})",small),
                p(f"出典：{link} / 選択理由：{row.get('selection_reason','')}",small)]))
    story += [PageBreak(),p("探索履歴・再現条件",title)]
    rounds=[r for r in assessment.get("rounds",[]) if not candidate_id or r.get("candidate_id")==candidate_id]
    if not rounds: story.append(p("反復探索はまだ行っていません。"))
    for item in rounds:
        story += [p(f"反復 {item.get('index','')} / 候補 {item.get('candidate_id','')} / {item.get('mode','local')}",heading),
                  p(f"方式：{item.get('embedding','')} 追加文献：{item.get('added_papers',0)}件 / 元の課題との類似度：{item.get('query_similarity_to_original','')}",small)]
        report=item.get("discovery_report",{})
        if report:
            story += [p(f"取得元：{report.get('provider','')} / 取得日時：{report.get('retrieved_at','')}",small),p("検索式："+report.get("query",""),small)]
        for note in item.get("notes",[]): story.append(p(note,small))
        if item.get("stop_reason"): story.append(p(item["stop_reason"],small))
    story += [p("来歴",heading),p(f"親評価：{assessment.get('parent_id') or '初回'}",small),
              p(f"数値ハッシュ：{assessment.get('numeric_hash','')}",small),
              p("PDFは保存済みの評価・評論を出力しました。出力時にLLMの再生成はしていません。全指標・欠測・フィードバック・原文は同じ評価IDのCSV/JSONで確認できます。",small)]
    stream=BytesIO()
    doc=SimpleDocTemplate(stream,pagesize=A4,rightMargin=42,leftMargin=42,topMargin=45,bottomMargin=48,
                         title="Research Atlas 探索・推薦レポート",author="Research Atlas")
    def page(canvas, document):
        canvas.setFillColor(navy);canvas.rect(0,A4[1]-10,A4[0],10,fill=1,stroke=0)
        canvas.setFont(font,7);canvas.setFillColor(muted)
        canvas.drawString(42,26,"RESEARCH ATLAS | "+assessment.get("id","")[:12])
        canvas.drawRightString(A4[0]-42,26,str(document.page))
    doc.build(story,onFirstPage=page,onLaterPages=page)
    return stream.getvalue()
