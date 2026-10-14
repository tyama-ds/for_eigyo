"""Saved full-corpus reports: bounded-memory data exports and Japanese PDF.

Exports never generate new LLM content. The caller supplies a stable report and
record snapshot; CSV/JSON include every record, including missing and failed ones.
"""
from __future__ import annotations

import csv
from html import escape
from io import BytesIO, StringIO
import json
from typing import Iterable, Iterator

from .foresight_exports import _font
from .reports import safe_cell


SCOPE_NOTE = ("選択した分析結果に収録された抄録を対象とするレポートです。論文本文の読解ではありません。"
              "全件の処理完了は、抽出・要約の完全性や科学的解釈の正しさを保証しません。"
              "階層要約では情報を圧縮するため、原文と全件のCSV / JSONも確認してください。")
STATUS = {"completed": "抽出済み", "missing": "抄録なし", "failed": "失敗", "pending": "未処理",
          "paused": "一時停止", "running": "処理中", "preparing": "台帳準備中", "synthesizing": "評論生成中",
          "ready": "抽出終了", "partial": "一部完了", "not_started": "未生成"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def iter_json(report: dict, records: Iterable[dict]) -> Iterator[str]:
    """Valid single JSON document, without materializing the paper iterator."""
    yield '{"report":' + _json(report) + ',"scope_note":' + _json(SCOPE_NOTE) + ',"papers":['
    separator = ""
    for record in records:
        yield separator + _json(record)
        separator = ","
    yield "]}"


CSV_FIELDS = ("row_type", "report_id", "result_id", "paper_id", "title", "year", "topic_id", "status",
              "fact_id", "kind", "statement", "quote", "source_start", "source_end", "numbers_json",
              "warnings_json", "error", "abstract", "details_json")


def iter_csv(report: dict, records: Iterable[dict]) -> Iterator[str]:
    """Tidy, formula-safe CSV; papers with no extracted facts still have a row."""
    stream = StringIO(newline="")
    writer = csv.writer(stream)

    def line(values):
        stream.seek(0)
        stream.truncate(0)
        writer.writerow([safe_cell(value) if isinstance(value, str) else value for value in values])
        return stream.getvalue()

    def row(row_type, record=None, **values):
        record = record or {}
        base = {"row_type": row_type, "report_id": report.get("id", ""), "result_id": report.get("result_id", ""),
                "paper_id": record.get("paper_id", record.get("id", "")), "title": record.get("title", ""),
                "year": record.get("year", ""), "topic_id": record.get("topic_id", ""),
                "status": record.get("status", "")}
        base.update(values)
        return line([base.get(key, "") for key in CSV_FIELDS])

    yield "\ufeff" + line(CSV_FIELDS)
    summaries = {"annual", "topics", "methods", "groups", "narrative"}
    yield row("report", statement=SCOPE_NOTE,
              details_json=_json({key: value for key, value in report.items() if key not in summaries}))
    for key in ("annual", "topics", "methods"):
        for item in report.get(key, []):
            yield row(key, item, title=item.get("label", item.get("method", "")), details_json=_json(item))

    def review_rows(value, label):
        if not value:
            return
        if isinstance(value, str):
            yield row("review", title=label, statement=value)
            return
        yield row("review", title=label, statement=value.get("text", ""),
                  warnings_json=_json(value.get("warnings", [])),
                  details_json=_json({key: item for key, item in value.items()
                                      if key not in {"sections", "paper_ids", "negative_results", "provenance", "input_contexts", "source_numbers", "source_quantities"}}))
        for section in value.get("sections", []):
            yield row("review_section", title=label + " / " + section.get("title", ""), statement=section.get("text", ""),
                      details_json=_json({key: item for key, item in section.items() if key not in {"paper_ids", "evidence_ids"}}))
            ids = section.get("paper_ids", section.get("evidence_ids", []))
            for offset in range(0, len(ids), 200):
                yield row("review_sources", title=label + " / " + section.get("title", ""), details_json=_json(ids[offset:offset + 200]))
        for item in value.get("negative_results", []):
            yield row("review_negative_evidence", title=label, details_json=_json(item))

    yield from review_rows(report.get("narrative"), "総合評論")
    for group in report.get("groups", []):
        label = str(group.get("label", group.get("key", "")))
        yield row("group", title=label, details_json=_json({key: item for key, item in group.items() if key not in {"summary", "narrative"}}))
        yield from review_rows(group.get("summary", group.get("narrative")), label)
    for record in records:
        extraction = record.get("extraction") or {}
        yield row("paper", record, abstract=record.get("abstract", ""), error=record.get("error", ""),
                  warnings_json=_json(extraction.get("warnings", [])),
                  details_json=_json({key: value for key, value in record.items() if key not in {"abstract", "extraction"}}))
        for fact in extraction.get("facts", []):
            yield row("fact", record, fact_id=fact.get("id", ""), kind=fact.get("kind", ""),
                      statement=fact.get("statement", ""), quote=fact.get("quote", ""),
                      source_start=fact.get("source_start", ""), source_end=fact.get("source_end", ""),
                      numbers_json=_json(fact.get("numbers", [])), warnings_json=_json(fact.get("warnings", [])),
                      details_json=_json(fact))
        for chunk in extraction.get("chunks", []):
            yield row("chunk", record, source_start=chunk.get("start", ""), source_end=chunk.get("end", ""),
                      status=chunk.get("status", ""), details_json=_json(chunk))


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _json(value) if isinstance(value, (dict, list)) else str(value)


def _warnings(value) -> list[str]:
    return [item if isinstance(item, str) else item.get("message", _text(item))
            for item in value if isinstance(item, (str, dict))]


def report_pdf(report: dict) -> bytes:
    """Integrated report, with every saved group review and its audit counts.

    Full evidence remains in CSV/JSON. A PDF is deliberately not a 20,000-paper
    dump; group reviews have no top-k exclusion here.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, CondPageBreak
        from reportlab.graphics.shapes import Drawing, Rect, String, Line
    except ImportError:
        raise ValueError("PDF出力には reportlab が必要です。requirements.txt をインストールしてください。") from None

    font = _font()
    navy, teal, muted, amber = (colors.HexColor(value) for value in ("#162B41", "#087F86", "#56697B", "#925311"))
    width = A4[0] - 88
    body = ParagraphStyle("corpus_body", fontName=font, fontSize=9, leading=15, textColor=navy,
                          wordWrap="CJK", spaceAfter=8)
    small = ParagraphStyle("corpus_small", parent=body, fontSize=7.5, leading=12, textColor=muted, spaceAfter=5)
    heading = ParagraphStyle("corpus_heading", parent=body, fontSize=13, leading=20, textColor=teal,
                             spaceBefore=13, spaceAfter=8, keepWithNext=True)
    title = ParagraphStyle("corpus_title", parent=body, fontSize=23, leading=32, spaceAfter=12)
    warning = ParagraphStyle("corpus_warning", parent=body, textColor=amber, backColor=colors.HexColor("#FFF5DD"),
                             borderPadding=6, spaceBefore=6, spaceAfter=12)

    def paragraph(value, style=body):
        clean = "".join(char for char in _text(value) if char in "\n\t" or ord(char) >= 32)
        return Paragraph(escape(clean).replace("\n", "<br/>"), style)

    def table(headers, rows, widths):
        data = [[paragraph(value, small) for value in headers]]
        data.extend([paragraph(value, small) for value in row] for row in rows)
        out = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        out.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E4F2F1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FA")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 0), (-1, 0), .7, colors.HexColor("#B9D6D6"))]))
        return out

    story = [paragraph("RESEARCH ATLAS  /  CORPUS REPORT", small), paragraph("全件抄録レポート", title)]
    counts = report.get("counts", {})
    subtitle = report.get("name")
    if not subtitle or subtitle == "全件抄録レポート":
        subtitle = "選択した分析結果に基づく総合評論"
    story += [paragraph(subtitle, heading),
              paragraph(f"作成日時: {report.get('created_at', '')}  /  更新日時: {report.get('updated_at', '')}", small),
              paragraph(f"Report: {report.get('id', '')}\nAnalysis: {report.get('result_id', '')}", small)]
    partial = bool(report.get("partial") or any(counts.get(key, 0) for key in ("pending", "failed", "missing")))
    if partial:
        story.append(paragraph("[!] 一部の抄録が未処理・失敗・欠測です。以下の評論は抽出できた範囲に基づきます。", warning))
    story.append(table(["対象論文", "抽出済み", "抄録なし", "失敗", "未処理"],
                       [[f"{counts.get(key, 0):,}" for key in ("total", "completed", "missing", "failed", "pending")]], [width / 5] * 5))
    story += [Spacer(1, 9), paragraph(SCOPE_NOTE, small),
              paragraph(f"接続先: {report.get('provider', '')} / モデル: {report.get('model', '')}\n"
                        f"抽出プロンプト版: {report.get('prompt_version', '')} / "
                        f"処理状態: {'今回の処理完了（部分結果）' if report.get('status') == 'completed' and partial else STATUS.get(report.get('status'), report.get('status', ''))}", small),
              paragraph(f"抄録の処理文字数: {counts.get('processed_chars', 0):,} / {counts.get('total_chars', 0):,} "
                        f"（引用・要約に採用された文字数とは異なります） / キャッシュ再利用: {counts.get('cached', 0):,} 件", small)]
    warnings = list(dict.fromkeys(_warnings(report.get("warnings", []))))
    if report.get("is_demo"):
        story.append(paragraph("[!] 合成・テストデータです。実際の研究動向を示すものではありません。", warning))
    if warnings:
        story.append(paragraph("対象とデータの注意", heading))
        for item in warnings:
            story.append(paragraph("・" + item, small))

    annual = report.get("annual", [])
    topics = report.get("topics", [])
    methods = report.get("methods", [])
    story.append(paragraph("計算による年次・分野集計", heading))
    story.append(paragraph("件数は取り込んだ対象集合の集計です。世界の研究動向全体の母数ではありません。手法の頻度は抽出語の表記を正規化した集計で、同義語の統合を保証しません。", small))
    if annual:
        # Split long date spans across readable charts, rather than squeezing labels.
        for offset in range(0, len(annual), 18):
            rows = annual[offset:offset + 18]
            plot = Drawing(width, 154)
            plot.add(Line(36, 28, width - 8, 28, strokeColor=colors.HexColor("#BAC8D4"), strokeWidth=.5))
            maximum = max([int(row.get("count", row.get("total", 0))) for row in rows] + [1])
            step = (width - 52) / len(rows)
            for index, row in enumerate(rows):
                value = int(row.get("count", row.get("total", 0)))
                x, height = 40 + index * step, value / maximum * 94
                plot.add(Rect(x + step * .15, 29, step * .6, height, fillColor=teal, strokeColor=None))
                plot.add(String(x + step * .45, height + 34, str(value), fontName=font, fontSize=7, textAnchor="middle", fillColor=navy))
                plot.add(String(x + step * .45, 14, str(row.get("year") or "不明"), fontName=font, fontSize=7, textAnchor="middle", fillColor=muted))
            chart_label = paragraph("年別の対象論文件数", small)
            chart_label.keepWithNext = True
            story += [chart_label, plot]
        story.append(table(["出版年", "対象", "抽出済み", "抄録なし", "失敗 / 未処理"],
            [[row.get("year") or "不明", row.get("count", row.get("total", 0)), row.get("completed", 0),
              row.get("missing", 0), f"{row.get('failed', 0)} / {row.get('pending', 0)}"] for row in annual],
            [width * .22, width * .17, width * .19, width * .19, width * .23]))
    if topics:
        story += [Spacer(1, 12), table(["分野", "対象", "抽出済み", "抄録なし", "失敗 / 未処理"],
            [[row.get("label", row.get("topic_id", "未分類")), row.get("count", row.get("total", 0)), row.get("completed", 0),
              row.get("missing", 0), f"{row.get('failed', 0)} / {row.get('pending', 0)}"] for row in topics],
            [width * .40, width * .13, width * .14, width * .14, width * .19])]
    if methods:
        story += [paragraph("抽出された研究手法", heading),
                  paragraph("PDFでは頻度順の上位30表記を示します。CSV / JSONには全表記を保存しています。", small),
                  table(["手法の表記", "論文件数"],
                        [[row.get("method", row.get("label", "")), row.get("paper_count", row.get("count", 0))] for row in methods[:30]],
                        [width * .8, width * .2])]

    def narrative_blocks(narrative, label):
        blocks = [paragraph(label, heading)]
        if not narrative:
            return blocks + [paragraph("この評論はまだ生成されていません。抽出済みの範囲で評論を生成できます。", small)]
        if isinstance(narrative, str):
            return blocks + [paragraph(narrative)]
        notices = _warnings(narrative.get("warnings", []))
        distinct = list(dict.fromkeys(notices))
        if notices:
            warning_count = max(len(notices), int(narrative.get("warning_count", len(notices))))
            blocks.append(paragraph(f"警告記録 {warning_count:,} 件。同じ内容をまとめて表示しています。詳細はJSON・各論文の抽出記録を参照してください。", small))
        for item in distinct[:20]:
            blocks.append(paragraph("[!] " + item, warning))
        if len(distinct) > 20:
            blocks.append(paragraph(f"ほか {len(distinct) - 20:,} 種類の警告はJSONに保存しています。", small))
        if narrative.get("text"):
            blocks.append(paragraph(narrative["text"]))
        for section in narrative.get("sections", []):
            if section.get("title"):
                blocks.append(paragraph(section["title"], heading))
            blocks.append(paragraph(section.get("text", "")))
            ids = section.get("paper_ids", section.get("evidence_ids", []))
            if ids:
                count = max(len(ids), int(section.get("paper_ids_count", len(ids))))
                blocks.append(paragraph("根拠ID: " + ", ".join(str(value) for value in ids[:8]) +
                                        (f" ほか {count - 8:,} 件（全IDはJSON参照）" if count > 8 else ""), small))
        return blocks

    story += [CondPageBreak(180), *narrative_blocks(report.get("narrative"), "LLMによる総合評論")]
    story.append(paragraph("研究の伸び・実用化の見通しに関する文章は、抽出根拠に基づく仮説です。予測精度を検証した確率やTRLの認定ではありません。", small))
    for group in report.get("groups", []):
        label = group.get("label") or f"{group.get('year') or '年不明'} / {group.get('topic_id', '未分類')}"
        if group.get("status") in {"stale", "failed", "partial"}:
            story.append(paragraph(f"[!] {label}: この章は更新待ち、または生成が完了していません。以前の評論がある場合は保持しています。", warning))
        story += [Spacer(1, 12), *narrative_blocks(group.get("summary", group.get("narrative")), str(label))]
    story += [paragraph("原典の確認と再利用", heading),
              paragraph("全件のCSVには論文ごとの状態、抽出した主張、対応する原文引用、数値・単位、原文の文字位置、分割処理の記録を保存します。JSONには集約・評論と全論文の処理記録を保存します。原文の位置は0から数え、終了位置の文字は含みません。", small),
              paragraph("抄録がない論文は内容分析の対象にできません。未処理・失敗を含む場合は再開または再試行し、評論を更新してください。未確認の数値や引用についての警告は、原論文で確認してください。", small)]

    def page(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D8E2E9"))
        canvas.line(44, 39, A4[0] - 44, 39)
        canvas.setFillColor(muted)
        canvas.setFont(font, 7)
        canvas.drawString(44, 26, "Research Atlas / 抄録に基づく分析" + (" / 部分レポート" if partial else ""))
        canvas.drawRightString(A4[0] - 44, 26, str(doc.page))
        canvas.restoreState()

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=44, rightMargin=44, topMargin=42, bottomMargin=54,
                            title="Research Atlas 全件抄録レポート", author="Research Atlas")
    doc.build(story, onFirstPage=page, onLaterPages=page)
    return output.getvalue()
