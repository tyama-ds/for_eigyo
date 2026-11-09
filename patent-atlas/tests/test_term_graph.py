import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis_engine import demo_rows
import term_graph


def labelled_rows():
    rows = demo_rows()
    for row in rows:
        if row["ipc"] in ("H01M10/0562", "H01M10/058", "H01M10/0565"):
            row["label"] = "keep"
        elif row["ipc"].startswith(("G06V", "H01L", "H01M10/052")):
            row["label"] = "exclude"
    return rows


class TermGraphTests(unittest.TestCase):
    def test_include_candidates_prefer_keep_only_terms_with_evidence_paths(self):
        rows = labelled_rows()
        graph = term_graph.TermGraph(rows)
        result = graph.suggest(dict(keywords=["電池"], classifications=[]), limit=6)
        self.assertEqual(result["keep_total"], 36)
        self.assertTrue(result["include"])
        first = result["include"][0]
        self.assertGreaterEqual(first["keep_hit"], 2)
        self.assertEqual(first["exclude_hit"], 0)
        self.assertEqual(first["path"][0], {"type": "term", "value": first["term"]})
        self.assertEqual(first["path"][1]["type"], "ipc")
        self.assertTrue(first["path"][-1]["ids"])
        self.assertNotIn("電池", [item["term"] for item in result["include"]])

    def test_exclude_candidates_never_occur_in_keep_rows(self):
        rows = labelled_rows()
        result = term_graph.TermGraph(rows).suggest(dict(keywords=["電池"], classifications=[]))
        self.assertTrue(result["exclude"])
        for item in result["exclude"]:
            self.assertEqual(item["keep_hit"], 0)
            self.assertGreaterEqual(item["exclude_hit"], 2)

    def test_or_groups_are_hypotheses_from_shared_keep_rows(self):
        rows = labelled_rows()
        result = term_graph.TermGraph(rows).suggest(dict(keywords=[], classifications=[]), limit=10)
        for group in result["or_groups"]:
            self.assertGreaterEqual(len(group["terms"]), 2)
            self.assertGreaterEqual(group["shared_keep"], 2)
            self.assertIn("同義語の確定ではありません", group["reason"])

    def test_too_few_labels_returns_empty_candidates(self):
        rows = demo_rows()
        rows[0]["label"] = "keep"
        result = term_graph.TermGraph(rows).suggest()
        self.assertEqual(result["include"], [])
        self.assertIn("2件以上", result["note"])

    def test_class_terms_describe_keep_rows_of_a_main_group(self):
        rows = labelled_rows()
        terms = term_graph.TermGraph(rows).class_terms("IPC:H01M10/0562")
        self.assertTrue(terms)
        self.assertTrue(all(item["keep_hit"] >= 2 for item in terms))

    def test_graph_suggestions_include_base_and_projected_scores(self):
        rows = labelled_rows()
        query = dict(keywords=["電池"], classifications=[], include_terms=[], exclude_terms=[])
        result = term_graph.graph_suggestions(rows, query, limit=4)
        self.assertEqual(result["base_score"]["keep_hit"], 36)
        for item in result["include"]:
            self.assertLessEqual(item["projected"]["keep_hit"], 36)
        for item in result["exclude"]:
            self.assertEqual(item["projected"]["keep_hit"], 36, "exclude terms never remove keep rows")


if __name__ == "__main__":
    unittest.main()
