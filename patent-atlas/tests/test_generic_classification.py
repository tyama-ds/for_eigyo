"""Live element -> official dictionary -> constrained review across unrelated fields."""

import copy
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import test_app as integration
import classification_retrieval as retrieval
import classification_planner as planner
import fi_catalog
import classification_catalog as ipc
import test_invention_routes as invention_tests

module = integration.module


def elements(name="膜分離", terms=None):
    return dict(
        elements=[
            dict(id="e1", name=name, terms=terms or ["membrane separation", "逆浸透"], relation="入力の分離機能")
        ],
        questions=[],
    )


def select(key="FI:B01D61/00", ids=None):
    return dict(
        selections=[dict(key=key, element_ids=ids or ["e1"], relation="broader", reason="膜分離を含む上位の定義")]
    )


class GenericDictionaryTests(unittest.TestCase):
    def test_unrelated_technical_elements_find_independent_ipc_and_fi(self):
        cases = [
            ("超音波検査", ["ultrasonic diagnosis", "超音波診断"], "A61B8/"),
            ("膜分離", ["membrane separation", "逆浸透"], "B01D61/"),
            ("半導体露光", ["photolithography"], "H10P76/"),
            ("繊維染色", ["dyeing textiles", "繊維の染色"], "D06"),
            ("農業灌漑", ["irrigation", "かんがい"], "E02B13/"),
            ("暗号通信", ["cryptographic", "暗号化"], "G06F21/"),
        ]
        for name, terms, prefix in cases:
            with self.subTest(name=name):
                rows = retrieval.retrieve_elements(elements(name, terms)["elements"], limit=48)
                for kind in ("IPC", "FI"):
                    self.assertTrue(any(r["kind"] == kind and r["code"].startswith(prefix) for r in rows), rows)
                self.assertTrue(all(r["verified"] and r["relevance_status"] == "review_pending" for r in rows))
                self.assertTrue(all(r["matched_elements"][0]["name"] == name for r in rows))

    def test_no_topic_profile_no_unrelated_fallback_and_specific_noun_is_required(self):
        self.assertEqual(retrieval.retrieve("unidentifiedquuxtechnology"), [])
        self.assertFalse(any(r["code"].startswith("F24F") for r in retrieval.retrieve("排水処理膜")))
        self.assertFalse(hasattr(retrieval, "EV_ENTRIES"))
        from discovery_engine import propose_plan

        self.assertEqual(propose_plan("未知の新規技術", [], [])["candidate_hypotheses"], [])
        self.assertEqual(propose_plan("全固体電池", [], [])["english_terms"], [])

    def test_all_domain_fi_hierarchy_concordance_and_independent_names(self):
        meta = fi_catalog.metadata()
        self.assertTrue(meta["complete"])
        self.assertGreater(meta["count"], 190000)
        self.assertEqual([r["code"] for r in fi_catalog.children()], list("ABCDEFGH"))
        self.assertEqual(fi_catalog.parent("B60L15/20@B")["code"], "B60L15/20@A")
        row = fi_catalog.lookup("B01D9/02 601A")
        self.assertIsNotNone(row)
        self.assertTrue(row["title_ja"])
        self.assertIn("B01D9/02", row["concordance_ipc"])
        self.assertIsNone(ipc.lookup("IPC", row["code"]))
        # Every imported parent exists; no cycles or fabricated digit parents.
        records = fi_catalog._data()["records"]
        for code, row in records.items():
            self.assertTrue(row[2] is None or row[2] in records, code)
            seen = {code}
            parent = row[2]
            while parent:
                self.assertNotIn(parent, seen)
                seen.add(parent)
                parent = records[parent][2]


class GenericClassificationApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(module.app)
        module.JOB.update(status="idle")
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        self.assertEqual(self.client.post("/api/demo").status_code, 200)

    def post(self):
        return self.client.post("/api/candidates", json={"keywords": "排水を膜で分離する装置", "use_llm": True})

    def test_two_stage_payloads_and_visible_element_provenance_preserve_user_data(self):
        before = copy.deepcopy(module.STATE)
        settings = copy.deepcopy(module.SETTINGS)
        with patch.object(module, "complete", side_effect=[elements(), select()]) as complete:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        first, second = complete.call_args_list
        self.assertNotIn("provided_candidates", first.args[2])
        allowed = {r["key"]: r for r in second.args[2]["provided_candidates"]}
        self.assertIn("FI:B01D61/00", allowed)
        self.assertTrue(allowed["FI:B01D61/00"]["definition"])
        report = response.json()["candidate_proposal"]
        self.assertEqual(report["elements"], elements()["elements"])
        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual(report["items"][0]["key"], "FI:B01D61/00")
        self.assertEqual(report["items"][0]["reviewed_element_ids"], ["e1"])
        self.assertTrue(report["items"][0]["matched_elements"])
        for field in ("selected", "patents", "queries", "training", "discovery"):
            self.assertEqual(module.STATE[field], before[field])
        self.assertEqual(module.SETTINGS, settings)

    def test_unprovided_code_or_malformed_decomposition_is_atomic(self):
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / "workspace.json").read_bytes()
        for replies in [
            [{"candidates": [{"kind": "IPC", "code": "B60L"}]}],
            [elements(), select("IPC:H99Z999/99")],
            [elements(), select(ids=["invented"])],
        ]:
            with patch.object(module, "complete", side_effect=replies):
                response = self.post()
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(module.STATE, before)
            self.assertEqual((module.DATA / "workspace.json").read_bytes(), disk)

    def test_no_definition_match_is_visible_and_does_not_invoke_empty_review(self):
        unknown = elements("未解明の要素", ["unidentifiedquuxtechnology"])
        with patch.object(module, "complete", return_value=unknown) as complete:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(complete.call_count, 1)
        report = response.json()["candidate_proposal"]
        self.assertEqual(report["accepted_count"], 0)
        self.assertEqual(report["unresolved_elements"], [dict(id="e1", name="未解明の要素")])

    def test_lexical_match_rejected_by_definition_review_is_not_promoted(self):
        with patch.object(module, "complete", side_effect=[elements(), dict(selections=[])]):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()["candidate_proposal"]
        self.assertGreater(report["retrieved_count"], 0)
        self.assertEqual(report["items"], [])
        self.assertTrue(report["unresolved_elements"])

    def test_two_stage_network_calls_release_lock_and_stale_result_cannot_commit(self):
        entered, release = threading.Event(), threading.Event()
        responses = []

        def model(_settings, _system, payload, **kwargs):
            if "provided_candidates" not in payload:
                return elements()
            entered.set()
            self.assertTrue(release.wait(5))
            return select()

        with patch.object(module, "complete", side_effect=model):
            thread = threading.Thread(target=lambda: responses.append(self.post()))
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                self.assertTrue(module.LOCK.acquire(timeout=0.5))
                module.LOCK.release()
                self.assertEqual(self.client.post("/api/selection", json={"selected": []}).status_code, 200)
                before = copy.deepcopy(module.STATE)
            finally:
                release.set()
                thread.join(10)
        self.assertEqual(responses[0].status_code, 409)
        self.assertEqual(module.STATE, before)


class GenericInventionRouteTests(unittest.TestCase):
    setUp = invention_tests.InventionRoutesTests.setUp
    post = invention_tests.InventionRoutesTests.post
    payload = invention_tests.InventionRoutesTests.payload

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def test_retrieved_seed_keeps_documentary_reason_and_separates_hypothesis(self):
        import classification_bridge

        patent = module.STATE["patents"][0]
        patent.update(ipc="", fi="B01D61/00")
        original = next(c for c in classification_bridge.extract(patent)["items"] if c["key"] == "FI:B01D61/00")
        result = self.post(
            "invention/plan",
            dict(
                brief=dict(
                    entry_mode="target",
                    purpose="分離技術の調査",
                    goal="膜分離",
                    keywords="膜分離",
                    user_aspects=[],
                    target_ids=[patent["id"]],
                ),
                invention_text="",
                manual_elements=["membrane separation"],
                use_llm=False,
            ),
        )
        plan = result["research_workbench"]["invention"]["plan"]
        row = next(c for c in plan["classification_candidates"] if c["key"] == "FI:B01D61/00")
        self.assertEqual(row["reason"], original["reason"])
        self.assertEqual(row["evidence_status"], "patent_metadata")
        self.assertTrue(row["hypothesis_reason"])
        self.assertEqual(row["classification_origins"], original["classification_origins"])

    def test_decomposition_precedes_retrieval_and_fi_query_keeps_explicit_seeds(self):
        body = dict(
            brief=dict(
                entry_mode="discover",
                purpose="膜分離装置の先行例",
                goal="排水を膜で分離する",
                keywords="排水処理",
                user_aspects=[],
                target_ids=[],
            ),
            invention_text="膜で水から溶質を分離する。",
            manual_elements=[],
            use_llm=True,
        )
        model = dict(
            purpose=body["brief"]["purpose"],
            summary="膜分離の要素から探す",
            questions=[],
            elements=[
                dict(
                    id="e1",
                    name="膜分離",
                    description="水から溶質を分離する膜",
                    relation="分離機能",
                    evidence=[dict(source_id="invention_text", quote="膜で水から溶質を分離する。")],
                    perspectives=[
                        dict(
                            id="e1p1",
                            name="膜の分離機能",
                            terms=["membrane separation", "逆浸透"],
                            classification_keys=[],
                            reason="分離原理から探す",
                        )
                    ],
                )
            ],
        )
        with (
            patch("invention_strategy.complete", return_value=model) as decompose,
            patch("llm.complete", return_value=select(ids=["e1p1"])) as review,
        ):
            result = self.post("invention/plan", body)
        plan = result["research_workbench"]["invention"]["plan"]
        initial = decompose.call_args.args[2]["provided_classifications"]
        self.assertFalse(any(c["key"] == "FI:B01D61/00" for c in initial))
        self.assertIn("FI:B01D61/00", [c["key"] for c in review.call_args.args[2]["provided_candidates"]])
        self.assertIn("FI:B01D61/00", plan["elements"][0]["perspectives"][0]["retrieved_classification_keys"])
        self.assertIn("IPC:B60", [c["key"] for c in plan["classification_candidates"]])
        selected = self.payload(plan)
        selected["elements"][0]["perspectives"][0].update(classification_keys=["FI:B01D61/00"], class_mode="class")
        preview = self.post("invention/preview", selected)
        self.assertEqual(preview["query"]["boolean_tree"], dict(op="class", system="FI", value="B01D61/00"))
        self.assertEqual(module.SETTINGS, self.settings)
