import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis_engine import demo_rows
from query_formats import Node, group, tree_data
import report_format


def plain_query():
    return dict(
        id="q1",
        version=2,
        keywords=["solid electrolyte", "battery"],
        classifications=[dict(kind="IPC", code="H01M10/0562", verified=True), dict(kind="IPC", code="H01M10/058", verified=True)],
        include_terms=["interface"],
        exclude_terms=["cooling"],
    )


class ReportFormatTests(unittest.TestCase):
    def test_steps_follow_the_and_not_structure_with_set_numbers(self):
        report = report_format.build_report(plain_query(), "derwent_innovation", options=dict(period_from=2013, period_to=2022, regions=["JP", "US"]))
        labels = [s["label"] for s in report["steps"]]
        self.assertEqual(labels[:2], ["キーワード", "キーワード"])
        self.assertEqual(labels[2], "分類（IPC）")
        self.assertEqual(labels[3], "キーワード")
        self.assertEqual(labels[4], "除外条件（NOT）")
        self.assertEqual(labels[-1], "母集団（集合の結合）")
        self.assertEqual(report["steps"][-1]["expression"], "(#1 AND #2 AND #3 AND #4) NOT #5")
        self.assertEqual(report["steps"][2]["expression"], "(IC=(H01M 10/0562) OR IC=(H01M 10/058))")
        self.assertTrue(report["can_copy"])
        self.assertIn("NOT", report["final_expression"])
        self.assertEqual(report["query_id"], "q1")

    def test_conditions_table_carries_period_regions_and_unit(self):
        report = report_format.build_report(
            plain_query(), "derwent_innovation",
            options=dict(period_from="2013", period_to="2022", basis="priority", regions="JP,US,WO", unit="family", search_date="2026-11-08"),
        )
        values = {c["label"]: c["value"] for c in report["conditions"]}
        self.assertIn("2013年〜2022年", values["対象期間"])
        self.assertIn("優先権主張年", values["対象期間"])
        self.assertEqual(values["対象国・地域"], "日本、米国、PCT 出願")
        self.assertIn("DWPI", values["集計単位"])
        self.assertEqual(values["検索日"], "2026-11-08")
        self.assertIn("Derwent Innovation", values["使用データベース"])

    def test_markdown_and_tsv_contain_the_tables(self):
        report = report_format.build_report(plain_query(), "derwent_innovation")
        self.assertIn("| No. | 条件 | 検索式 | ヒット件数 |", report["markdown"])
        self.assertIn("### 調査条件", report["markdown"])
        self.assertIn("（記入）", report["markdown"])
        self.assertIn("最終式", report["markdown"])
        self.assertTrue(report["tsv"].startswith("項目\t内容\n"))
        self.assertIn("\t除外条件（NOT）\t", report["tsv"])

    def test_local_hits_are_counted_per_step_when_rows_are_given(self):
        rows = demo_rows()
        query = dict(id="q2", version=1, keywords=["電池"], classifications=[dict(kind="IPC", code="H01M10/0562", verified=True)], include_terms=[], exclude_terms=[])
        report = report_format.build_report(query, "jplatpat", rows=rows)
        self.assertEqual(report["local_rows"], 72)
        by_label = {s["label"]: s for s in report["steps"]}
        self.assertEqual(by_label["分類（IPC）"]["local_hits"], 12)
        self.assertEqual(by_label["母集団（集合の結合）"]["local_hits"], 12)
        self.assertEqual(by_label["分類（IPC）"]["expression"], "H01M10/0562/IP")
        self.assertIn("取込CSV該当数（参考）", report["markdown"])

    def test_facet_names_label_concept_steps(self):
        tree = Node("not", children=(
            group("and", [group("or", [Node("text", "走行"), Node("text", "移動")]), Node("class", "B60W60/00", "IPC")]),
            group("or", [Node("text", "液体電解質")]),
        ))
        query = dict(
            id="q3", version=3, boolean_tree=tree_data(tree),
            classifications=[dict(kind="IPC", code="B60W60/00", verified=True)],
            facets=[dict(name="移動の仕組み", terms=["走行", "移動"], role="required"), dict(name="液系の除外", terms=["液体電解質"], role="exclude")],
        )
        report = report_format.build_report(query, "jplatpat")
        self.assertEqual(report["steps"][0]["label"], "観点: 移動の仕組み")
        self.assertEqual(report["steps"][0]["expression"], "[走行/TX+移動/TX]")
        self.assertEqual(report["steps"][2]["label"], "除外条件（NOT）")
        self.assertEqual(report["steps"][-1]["expression"], "(#1 AND #2) NOT #3")

    def test_invalid_options_and_held_output_are_reported(self):
        with self.assertRaises(ValueError):
            report_format.build_report(plain_query(), "derwent_innovation", options=dict(period_from="20x3"))
        with self.assertRaises(ValueError):
            report_format.build_report(plain_query(), "derwent_innovation", options=dict(period_from=2020, period_to=2010))
        with self.assertRaises(ValueError):
            report_format.build_report(plain_query(), "nowhere")
        query = plain_query()
        query["keywords"] = ["固体電解質"]
        report = report_format.build_report(query, "derwent_innovation")
        self.assertFalse(report["can_copy"])
        self.assertIsNone(report["final_expression"])
        self.assertTrue(report["problems"])
        self.assertIn("出力を保留している事項", report["markdown"])


if __name__ == "__main__":
    unittest.main()
