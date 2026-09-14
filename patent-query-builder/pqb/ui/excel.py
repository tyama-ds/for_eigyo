"""Excel 設計シート（付録B）の生成と読み戻し。標準ライブラリのみの xlsx 書き出し／読み込み。

- 数式は使わず値のみ。列名で照合し、セル位置に依存しない（§9.8・§14.2）
- 人が編集するのは「採否」「理由コード」「備考」「人の総合」「人の観点別」「コメント」列だけ
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

SHEETS = ["案件", "観点", "語候補", "分類候補", "検索式", "実行結果", "判定", "変換候補", "ログ"]

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


# ================================================================== 書き出し

def _col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _cell_xml(ref: str, value, header: bool) -> str:
    style = ' s="1"' if header else ""
    if value is None or value == "":
        return f'<c r="{ref}"{style}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{style}><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = escape(str(value)).replace("\r", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(rows: list[list]) -> str:
    widths = {}
    for row in rows:
        for j, v in enumerate(row):
            ln = len(str(v)) if v is not None else 0
            widths[j] = max(widths.get(j, 8), min(60, ln * 1.6 + 2))
    cols = "".join(f'<col min="{j + 1}" max="{j + 1}" width="{w:.1f}" customWidth="1"/>' for j, w in sorted(widths.items()))
    body = []
    for i, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(f"{_col_letter(j)}{i}", v, i == 1) for j, v in enumerate(row))
        body.append(f'<row r="{i}">{cells}</row>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<cols>{cols}</cols><sheetData>{"".join(body)}</sheetData></worksheet>')


def write_xlsx(sheets: dict[str, list[list]]) -> bytes:
    buf = io.BytesIO()
    names = list(sheets)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(len(names)))
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   f'{overrides}</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        sheets_xml = "".join(f'<sheet name="{escape(n[:31])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<sheets>{sheets_xml}</sheets></workbook>')
        rels = "".join(
            f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
            for i in range(len(names)))
        rels += f'<Relationship Id="rId{len(names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/></patternFill></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
                   '</styleSheet>')
        for i, n in enumerate(names):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(sheets[n]))
    return buf.getvalue()


# ================================================================== 読み込み

def _ref_to_col(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(data: bytes) -> dict[str, list[list]]:
    out: dict[str, list[list]] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        for sheet in wb.find("m:sheets", _NS):
            rid = sheet.get(f"{{{_NS['r']}}}id")
            target = targets.get(rid, "")
            path = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
            root = ET.fromstring(z.read(path))
            rows: list[list] = []
            for row in root.iter(f"{{{_NS['m']}}}row"):
                values: dict[int, object] = {}
                for c in row.findall("m:c", _NS):
                    col = _ref_to_col(c.get("r", "A1"))
                    t = c.get("t")
                    v = c.find("m:v", _NS)
                    if t == "inlineStr":
                        is_ = c.find("m:is", _NS)
                        val = "".join(x.text or "" for x in is_.iter(f"{{{_NS['m']}}}t")) if is_ is not None else ""
                    elif t == "s":
                        val = shared[int(v.text)] if v is not None else ""
                    elif t == "b":
                        val = bool(int(v.text)) if v is not None else None
                    elif v is None:
                        val = None
                    elif t == "str":
                        val = v.text or ""
                    else:
                        txt = v.text or ""
                        try:
                            val = int(txt) if re.fullmatch(r"-?\d+", txt) else float(txt)
                        except ValueError:
                            val = txt
                    values[col] = val
                width = max(values) + 1 if values else 0
                rows.append([values.get(j) for j in range(width)])
            out[sheet.get("name")] = rows
    return out


def rows_to_dicts(rows: list[list]) -> list[dict]:
    if not rows:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(v not in (None, "") for v in r):
            continue
        out.append({header[j]: (r[j] if j < len(r) else None) for j in range(len(header)) if header[j]})
    return out


# ================================================================== 設計シート

def _fmt(v):
    if isinstance(v, float):
        return round(v, 4)
    return v


def design_workbook(bundle: dict) -> bytes:
    """orchestrator.case_bundle() の内容から付録B の 9 シートを作る。"""
    case = bundle["case"]
    sheets: dict[str, list[list]] = {}
    sheets["案件"] = [["案件ID", "案件名", "調査種別", "入力文", "既知文献（改行区切り）", "期間（自）", "期間（至）", "国", "担当", "作成日", "状態", "反復"],
                     [case["case_id"], case.get("name", ""), case.get("purpose", ""), case.get("input_text", ""),
                      "\n".join(case.get("seeds", [])), case.get("date_from", ""), case.get("date_to", ""),
                      ",".join(case.get("countries", [])), case.get("actor", ""), case.get("created_at", ""),
                      case.get("status", ""), case.get("iteration", 1)]]
    sheets["観点"] = [["観点ID", "観点名", "種別（必須／補助）", "定義（一文）", "由来（P1／人）", "根拠（入力文）", "確定フラグ"]]
    for a in bundle["axes"]:
        sheets["観点"].append([a["axis_id"], a["name"], "必須" if a["kind"] == "required" else "補助", a.get("definition", ""),
                               a.get("origin", ""), a.get("evidence", ""), "済" if a.get("fixed") else ""])
    sheets["語候補"] = [["候補ID", "反復", "観点ID", "語", "表記種別", "由来", "由来文献", "RSJ_w", "OW", "r", "n", "R", "N", "フリップ率", "要確認",
                         "採否（採用／棄却／保留）", "理由コード", "備考"]]
    sheets["分類候補"] = [["候補ID", "反復", "観点ID", "体系（FI／FT／IPC）", "コード", "粒度", "タイトル（辞書）", "辞書照合", "由来", "既知文献での出現数",
                          "RSJ_w", "OW", "r", "n", "フリップ率", "要確認", "採否（採用／棄却／保留）", "理由コード", "備考"]]
    status_ja = {"adopted": "採用", "rejected": "棄却", "candidate": "保留"}
    for c in bundle["candidates"]:
        if c["kind"] == "term":
            sheets["語候補"].append([c["candidate_id"], c.get("iteration"), c["axis_id"], c["value"], c.get("variant_kind", ""),
                                     c.get("origin", ""), c.get("origin_doc", ""), _fmt(c.get("rsj_w")), _fmt(c.get("offer_w")),
                                     c.get("r"), c.get("n"), c.get("R"), c.get("N"), _fmt(c.get("flip_rate")),
                                     "要確認" if c.get("needs_review") else "", status_ja.get(c["status"], c["status"]),
                                     c.get("reason_code", ""), c.get("note", "")])
        else:
            sheets["分類候補"].append([c["candidate_id"], c.get("iteration"), c["axis_id"], c.get("scheme", ""), c["value"], c.get("level", ""),
                                       c.get("title", ""), {True: "済", False: "未照合", None: ""}.get(c.get("dict_known"), ""),
                                       c.get("origin", ""), c.get("origin_doc", ""), _fmt(c.get("rsj_w")), _fmt(c.get("offer_w")),
                                       c.get("r"), c.get("n"), _fmt(c.get("flip_rate")), "要確認" if c.get("needs_review") else "",
                                       status_ja.get(c["status"], c["status"]), c.get("reason_code", ""), c.get("note", "")])
    sheets["検索式"] = [["版", "案（広め／標準／狭め）", "方言", "分割No", "式文字列", "文字数", "往復検証", "生成日時", "DSL 参照"]]
    variant_ja = {"broad": "広め", "standard": "標準", "narrow": "狭め"}
    for q in bundle["queries"]:
        for r in q.get("renderings", []):
            sheets["検索式"].append([q["iteration"], variant_ja.get(q["variant"], q["variant"]), r["dialect"], r["part_no"], r["text"], r["chars"],
                                     "OK" if r["roundtrip_ok"] else "NG", q.get("created_at", ""), q["query_id"]])
    sheets["実行結果"] = [["版", "案", "DB／取得元", "件数", "CSVパス", "実行日時", "既知文献再現率", "プール再現率", "上位K適合率", "推定再現率", "CI下限", "CI上限"]]
    for it in bundle["iterations"]:
        m = it.get("metrics", {})
        for variant, vm in (m.get("variants") or {}).items():
            est = (m.get("estimates") or {}).get(variant) or {}
            sheets["実行結果"].append([it["iteration"], variant_ja.get(variant, variant), vm.get("source", ""), vm.get("hit_count"),
                                       vm.get("csv_path", ""), vm.get("executed_at", ""), _fmt(vm.get("recall_seed")), _fmt(vm.get("recall_pool")),
                                       _fmt(vm.get("p_at_k")), _fmt(est.get("recall_hat")), _fmt(est.get("ci_low")), _fmt(est.get("ci_high"))])
    sheets["判定"] = [["文献番号", "名称", "版", "抽出理由（上位K／標本／引用）", "LLM総合", "LLM観点別", "フリップ率", "人の総合", "人の観点別", "コメント"]]
    for j in bundle["judgment_table"]:
        sheets["判定"].append([j["doc_id"], j.get("title", ""), j.get("iteration"), j.get("selection", ""), j.get("llm_overall"),
                               j.get("llm_per_axis", ""), _fmt(j.get("flip_rate")), j.get("human_overall"), j.get("human_per_axis", ""),
                               j.get("comment", "")])
    sheets["変換候補"] = [["ID", "版", "操作", "対象", "発生源（RSJ／決定木／LLM）", "方向", "予測母集団サイズ", "予測プール再現率", "予測P@K", "局所評価済み", "退行", "理由", "採否"]]
    for t in bundle["transforms"]:
        sheets["変換候補"].append([t["transform_id"], t["iteration"], t["op"], _target_text(t.get("target", {})), t.get("source", ""), t.get("direction", ""),
                                   t.get("pred_hits"), _fmt(t.get("pred_recall_pool")), _fmt(t.get("pred_p_at_k")), "済" if t.get("local_eval") else "",
                                   "退行" if t.get("regression") else "", t.get("reason", ""), status_ja.get(t["status"], t["status"])])
    sheets["ログ"] = [["日時", "版", "ゲート", "決定者", "操作", "内容"]]
    for lg in bundle["logs"]:
        sheets["ログ"].append([lg["created_at"], lg.get("iteration"), lg.get("gate", ""), lg.get("actor", ""), lg.get("action", ""), lg.get("message", "")])
    return write_xlsx(sheets)


def _target_text(target: dict) -> str:
    parts = []
    for k in ("axis_id", "text", "scheme", "code", "new_code", "join", "fields", "other_axis_id"):
        if k in target and target[k] not in (None, "", []):
            parts.append(f"{k}={target[k]}")
    return " ".join(parts)


def parse_design_workbook(data: bytes) -> dict:
    """人が編集したシートから判断を取り出す（列名で照合）。"""
    sheets = read_xlsx(data)
    status_map = {"採用": "adopted", "棄却": "rejected", "保留": "candidate", "adopted": "adopted", "rejected": "rejected"}
    decisions = []
    for name in ("語候補", "分類候補"):
        for row in rows_to_dicts(sheets.get(name, [])):
            cid = row.get("候補ID")
            status = status_map.get(str(row.get("採否（採用／棄却／保留）") or "").strip())
            if cid and status:
                decisions.append({"candidate_id": str(cid), "status": status,
                                  "reason_code": str(row.get("理由コード") or "").strip().zfill(2) if row.get("理由コード") not in (None, "") else "",
                                  "note": str(row.get("備考") or "")})
    judgments = []
    for row in rows_to_dicts(sheets.get("判定", [])):
        val = row.get("人の総合")
        if row.get("文献番号") and val not in (None, ""):
            try:
                overall = int(float(val))
            except (TypeError, ValueError):
                continue
            per_axis = {}
            raw = str(row.get("人の観点別") or "")
            for part in re.split(r"[;,、 ]+", raw):
                if ":" in part or "=" in part:
                    k, v = re.split(r"[:=]", part, 1)
                    try:
                        per_axis[k.strip()] = int(float(v))
                    except ValueError:
                        pass
            judgments.append({"doc_id": str(row["文献番号"]), "overall": overall, "per_axis": per_axis,
                              "comment": str(row.get("コメント") or "")})
    transforms = []
    for row in rows_to_dicts(sheets.get("変換候補", [])):
        status = status_map.get(str(row.get("採否") or "").strip())
        if row.get("ID") and status in ("adopted", "rejected"):
            transforms.append({"transform_id": str(row["ID"]), "status": status})
    return {"decisions": decisions, "judgments": judgments, "transforms": transforms}
