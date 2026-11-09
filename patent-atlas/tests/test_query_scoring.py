import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis_engine import demo_rows
from query_formats import Node, group, tree_data
import query_scoring as scoring


def labelled_rows():
    rows = demo_rows()
    for row in rows:
        if row["ipc"] in ("H01M10/0562", "H01M10/058", "H01M10/0565"):
            row["label"] = "keep"
        elif row["ipc"].startswith(("G06V", "H01L")):
            row["label"] = "exclude"
    return rows


class RowCodesTests(unittest.TestCase):
    def test_parses_each_system_and_skips_malformed_fields(self):
        codes = scoring.row_codes({"ipc": "H01M10/0562; H01M4/00", "fterm": "5H029AJ06", "fi": "H01M 10/0562 A", "cpc": "???"})
        self.assertEqual(codes["IPC"], {"H01M10/0562", "H01M4/00"})
        self.assertEqual(codes["F-term"], {"5H029AJ06"})
        self.assertIn("H01M10/0562A", codes["FI"])
        self.assertEqual(codes["CPC"], set())


class EvaluateTests(unittest.TestCase):
    row = {"id": "X", "title": "固体電解質の電池", "abstract": "界面抵抗を低減する。", "ipc": "H01M10/0562", "fterm": "5H029AJ06"}

    def test_text_and_class_nodes(self):
        self.assertTrue(scoring.evaluate(Node("text", "界面抵抗"), self.row))
        self.assertFalse(scoring.evaluate(Node("text", "冷却"), self.row))
        self.assertTrue(scoring.evaluate(Node("class", "H01M10/0562", "IPC"), self.row))
        self.assertTrue(scoring.evaluate(Node("class", "H01M", "IPC"), self.row), "upper IPC covers subgroups")
        self.assertTrue(scoring.evaluate(Node("class", "H01M10/00", "IPC"), self.row), "main group covers subgroups")
        self.assertFalse(scoring.evaluate(Node("class", "H01M4/00", "IPC"), self.row))
        self.assertTrue(scoring.evaluate(Node("class", "5H029AJ06", "F-term"), self.row))

    def test_boolean_operators(self):
        tree = Node("not", children=(group("and", [Node("text", "電池"), Node("class", "H01M", "IPC")]), Node("text", "冷却")))
        self.assertTrue(scoring.evaluate(tree, self.row))
        tree = Node("not", children=(Node("text", "電池"), Node("text", "界面")))
        self.assertFalse(scoring.evaluate(tree, self.row))


class ScoreQueryTests(unittest.TestCase):
    def test_counts_keep_and_exclude_separately(self):
        rows = labelled_rows()
        query = dict(keywords=["電池"], classifications=[], include_terms=[], exclude_terms=[])
        score = scoring.score_query(query, rows)
        self.assertEqual(score["keep_total"], 36)
        self.assertEqual(score["exclude_total"], 24)
        self.assertEqual(score["keep_hit"], 36)
        self.assertEqual(score["exclude_hit"], 0, "demo exclude rows do not mention 電池")
        self.assertEqual(score["keep_recall"], 1.0)
        self.assertEqual(score["missed_keep_ids"], [])
        self.assertIn("再計算", score["note"])

    def test_include_and_exclude_terms_change_the_counts(self):
        rows = labelled_rows()
        query = dict(keywords=["電池"], classifications=[], include_terms=[], exclude_terms=[])
        narrowed = scoring.score_query(query, rows, include=["硫化物"])
        self.assertLess(narrowed["keep_hit"], 36)
        self.assertGreater(narrowed["keep_hit"], 0)
        self.assertTrue(narrowed["missed_keep_ids"])
        delta = scoring.compare(scoring.score_query(query, rows), narrowed)
        self.assertLess(delta["keep_hit"], 0)

    def test_boolean_tree_queries_are_scored_with_the_tree(self):
        rows = labelled_rows()
        tree = tree_data(group("and", [Node("text", "電池"), Node("class", "H01M10/0562", "IPC")]))
        score = scoring.score_query(dict(boolean_tree=tree), rows)
        self.assertEqual(score["keep_hit"], 12)
        self.assertEqual(score["exclude_hit"], 0)

    def test_invalid_query_reports_error_instead_of_raising(self):
        score = scoring.score_query(dict(keywords=[], classifications=[]), labelled_rows())
        self.assertIn("error", score)


if __name__ == "__main__":
    unittest.main()
