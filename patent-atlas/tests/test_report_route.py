"""POST /api/query/report returns the report tables for a saved query."""

import os
import tempfile
import unittest

TEMP = tempfile.TemporaryDirectory(prefix="patent-atlas-report-test-")
os.environ.setdefault("PATENT_ATLAS_DATA", TEMP.name)
from fastapi.testclient import TestClient
import app as module


class ReportRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        module.JOB.update(status="idle")
        self.assertEqual(self.client.post("/api/demo").status_code, 200)

    def test_report_for_the_latest_query_in_jplatpat_syntax(self):
        query_id = module.STATE["queries"][-1]["id"]
        body = dict(query_id=query_id, format="jplatpat", options=dict(period_from=2015, period_to=2024, regions="JP"))
        response = self.client.post("/api/query/report", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["query_id"], query_id)
        self.assertEqual(report["syntax"], "jplatpat")
        self.assertEqual(report["local_rows"], 72)
        labels = [s["label"] for s in report["steps"]]
        self.assertIn("分類（F-term / IPC）", labels)
        self.assertEqual(labels[-1], "母集団（集合の結合）")
        self.assertTrue(all("/TX" in s["expression"] for s in report["steps"] if s["label"] == "キーワード"))
        self.assertIn("2015年〜2024年", report["markdown"])
        self.assertIn("日本", report["markdown"])
        self.assertTrue(report["can_copy"])

    def test_missing_query_and_invalid_options_are_rejected(self):
        self.assertEqual(self.client.post("/api/query/report", json={"query_id": "nope"}).status_code, 404)
        query_id = module.STATE["queries"][-1]["id"]
        bad = self.client.post("/api/query/report", json={"query_id": query_id, "options": {"basis": "x"}})
        self.assertEqual(bad.status_code, 400)
        without_rows = self.client.post("/api/query/report", json={"query_id": query_id, "local_hits": False}).json()
        self.assertIsNone(without_rows["local_rows"])
        self.assertNotIn("取込CSV該当数", without_rows["markdown"])


if __name__ == "__main__":
    unittest.main()
