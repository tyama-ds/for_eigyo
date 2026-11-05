"""The refinement stage exposes graph suggestions and retrospective scores through the API."""

import os
import tempfile
import unittest

TEMP = tempfile.TemporaryDirectory(prefix="patent-atlas-graph-test-")
os.environ.setdefault("PATENT_ATLAS_DATA", TEMP.name)
from fastapi.testclient import TestClient
import app as module


class RefinementGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        module.JOB.update(status="idle")
        self.assertEqual(self.client.post("/api/demo").status_code, 200)
        rows = module.STATE["patents"]
        keep = [r["id"] for r in rows if r["ipc"] in ("H01M10/0562", "H01M10/058", "H01M10/0565")]
        exclude = [r["id"] for r in rows if r["ipc"].startswith(("G06V", "H01L", "H01M10/052"))]
        self.assertEqual(self.client.post("/api/labels", json={"ids": keep, "label": "keep"}).status_code, 200)
        self.assertEqual(self.client.post("/api/labels", json={"ids": exclude, "label": "exclude"}).status_code, 200)

    def test_refinement_returns_graph_candidates_with_evidence_and_base_score(self):
        result = self.client.get("/api/refinement").json()
        graph = result["graph"]
        self.assertEqual(graph["keep_total"], 36)
        self.assertTrue(graph["include"])
        first = graph["include"][0]
        self.assertEqual(first["exclude_hit"], 0)
        self.assertEqual(first["path"][-1]["type"], "patents")
        self.assertIn("projected", first)
        # The demo query ANDs three phrases, so few labelled rows match; totals are what matter here.
        self.assertEqual(result["score"]["keep_total"], 36)
        self.assertEqual(result["score"]["exclude_total"], 36)
        self.assertEqual(result["score"]["query_id"], module.STATE["queries"][-1]["id"])
        # Graph terms are offered alongside the TF-IDF terms in the same lists.
        self.assertIn(first["term"], result["include"])
        for item in graph["exclude"]:
            self.assertIn(item["term"], result["exclude"])

    def test_score_endpoint_projects_chosen_terms(self):
        graph = self.client.get("/api/refinement").json()["graph"]
        include = [graph["include"][0]["term"]]
        exclude = [graph["exclude"][0]["term"]] if graph["exclude"] else []
        result = self.client.post("/api/refinement/score", json={"include": include, "exclude": exclude}).json()
        self.assertEqual(result["base"]["keep_total"], 36)
        self.assertLessEqual(result["projected"]["keep_hit"], result["base"]["keep_hit"])
        self.assertLessEqual(result["projected"]["exclude_hit"], result["base"]["exclude_hit"])
        self.assertEqual(result["delta"]["keep_hit"], result["projected"]["keep_hit"] - result["base"]["keep_hit"])
        bad = self.client.post("/api/refinement/score", json={"include": "x"})
        self.assertEqual(bad.status_code, 400)

    def test_graph_suggested_terms_are_accepted_by_the_refined_query(self):
        graph = self.client.get("/api/refinement").json()["graph"]
        term = graph["include"][0]["term"]
        response = self.client.post("/api/query", json={"refine": True, "include": [term], "exclude": []})
        self.assertEqual(response.status_code, 200, response.text)
        latest = response.json()["queries"][-1]
        self.assertEqual(latest["include_terms"], [term])
        self.assertIn(term, latest["expression"])


if __name__ == "__main__":
    unittest.main()
