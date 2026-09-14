import unittest

from pqb.ui.excel import SHEETS, parse_design_workbook, read_xlsx, rows_to_dicts, write_xlsx


class TestXlsx(unittest.TestCase):
    def test_write_read_roundtrip(self):
        data = write_xlsx({"語候補": [["候補ID", "語", "RSJ_w", "採否（採用／棄却／保留）", "flag"], ["c1", "高強度鋼板", 1.5, "採用", True], ["c2", "鋼板", None, "棄却", False]],
                           "ログ": [["日時", "内容"], ["2026-01-01", "x\ny<>&\"'"]]})
        self.assertTrue(data.startswith(b"PK"))
        back = read_xlsx(data)
        self.assertEqual(list(back), ["語候補", "ログ"])
        rows = rows_to_dicts(back["語候補"])
        self.assertEqual(rows[0]["RSJ_w"], 1.5)
        self.assertIs(rows[0]["flag"], True)
        self.assertIsNone(rows[1]["RSJ_w"])
        self.assertEqual(back["ログ"][1][1], "x\ny<>&\"'")

    def test_design_and_parse(self):
        from helpers import sample_query
        q = sample_query()
        bundle = {
            "case": {"case_id": "C1", "name": "n", "purpose": "prior_art", "input_text": "x", "seeds": ["JP1"], "countries": ["JP"],
                     "date_from": "", "date_to": "", "actor": "human", "created_at": "t", "status": "g2_pending", "iteration": 1},
            "axes": [{"axis_id": "A", "name": "対象", "kind": "required", "definition": "", "origin": "", "evidence": "", "fixed": True}],
            "candidates": [{"candidate_id": "c1", "kind": "term", "axis_id": "A", "value": "高強度鋼板", "status": "candidate", "iteration": 1},
                           {"candidate_id": "c2", "kind": "code", "axis_id": "A", "value": "C22C38/00", "scheme": "FI", "status": "candidate", "iteration": 1, "dict_known": True}],
            "queries": [{"query_id": q.query_id, "iteration": 1, "variant": "standard", "created_at": "t", "renderings": [{"dialect": "jplatpat", "part_no": 1, "text": "[x]", "chars": 3, "roundtrip_ok": True}]}],
            "iterations": [{"iteration": 1, "metrics": {"variants": {"standard": {"hit_count": 10, "recall_seed": 1.0, "source": "csv"}}, "estimates": {}}}],
            "judgment_table": [{"doc_id": "D1", "title": "t", "iteration": 1, "selection": "topk", "llm_overall": 2, "llm_per_axis": "A:2", "flip_rate": 0.0}],
            "transforms": [{"transform_id": "tf1", "iteration": 1, "op": "ADD_TERM", "target": {"axis_id": "A", "text": "x"}, "source": "rsj", "status": "candidate"}],
            "logs": [{"created_at": "t", "iteration": 1, "gate": "G1", "actor": "human", "action": "a", "message": "m"}],
        }
        from pqb.ui.excel import design_workbook
        data = design_workbook(bundle)
        sheets = read_xlsx(data)
        self.assertEqual(list(sheets), SHEETS)
        # 人が編集したことにして読み戻す
        edited = {name: rows for name, rows in sheets.items()}
        hdr = edited["語候補"][0]
        i_st, i_rc = hdr.index("採否（採用／棄却／保留）"), hdr.index("理由コード")
        edited["語候補"][1][i_st] = "採用"
        edited["語候補"][1][i_rc] = 1
        hdr2 = edited["判定"][0]
        edited["判定"][1][hdr2.index("人の総合")] = 3
        edited["判定"][1][hdr2.index("人の観点別")] = "A:3"
        hdr3 = edited["変換候補"][0]
        edited["変換候補"][1][hdr3.index("採否")] = "採用"
        parsed = parse_design_workbook(write_xlsx(edited))
        self.assertEqual(parsed["decisions"][0], {"candidate_id": "c1", "status": "adopted", "reason_code": "01", "note": ""})
        self.assertEqual(parsed["judgments"][0]["overall"], 3)
        self.assertEqual(parsed["judgments"][0]["per_axis"], {"A": 3})
        self.assertEqual(parsed["transforms"][0]["status"], "adopted")


if __name__ == "__main__":
    unittest.main()
