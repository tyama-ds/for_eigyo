"""Explicit route services and classification dependencies keep existing contracts."""

import ast
import copy
import importlib
import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import classification_bridge
import classification_explorer as explorer
import classification_symbols
from research_services import ResearchServices
from services import Services
from workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]
REGISTRATIONS = ("discovery", "orchestration", "convergence", "invention", "research")


def services_for(ws):
    callbacks = {field.name: Mock(name=field.name) for field in fields(Services)}
    callbacks.update(save=ws.save, public_state=ws.public_state)
    return Services(**callbacks)


class ServiceDependencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="patent-atlas-services-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def make_workspace(self, name="one", state=None):
        return Workspace(
            self.root / name,
            state=state if state is not None else {"keywords": "材料", "patents": []},
            settings={"provider": "offline"},
            job={"status": "idle"},
        )

    def test_registration_neither_initializes_state_nor_calls_services(self):
        ws = self.make_workspace()
        before = copy.deepcopy((ws.state, ws.settings, ws.job))
        callbacks = {
            field.name: Mock(side_effect=AssertionError("registration must be read-only")) for field in fields(Services)
        }
        services = Services(**callbacks)
        research = ResearchServices(ws, services)
        app = FastAPI()
        for name in REGISTRATIONS:
            module = importlib.import_module(name + "_routes")
            getattr(module, "register_" + name + "_routes")(
                app, ws, research if name in ("research", "invention") else services
            )
        self.assertEqual((ws.state, ws.settings, ws.job), before)
        self.assertEqual(list(ws.data_dir.iterdir()), [])

    def test_load_initializes_and_recovers_workflows_without_writing(self):
        ws = self.make_workspace()
        ws.load(initialize_workflows=True)
        self.assertEqual(ws.state["research_workbench"]["brief"]["keywords"], "材料")
        self.assertEqual(ws.state["orchestration"]["status"], "idle")
        self.assertEqual(list(ws.data_dir.iterdir()), [])
        ws.state["orchestration"].update(status="running", stage="train")
        ws.state["research_workbench"]["target_patents"] = [{"id": "kept-reference"}]
        ws.state["extension"] = {"preserve": True}
        ws.save()
        target = ws.data_dir / "workspace.json"
        original = target.read_bytes()
        before = copy.deepcopy(ws.state["research_workbench"])
        ws.load(initialize_workflows=True)
        self.assertEqual(ws.state["orchestration"]["status"], "paused")
        self.assertEqual(ws.state["orchestration"]["stage"], "train")
        self.assertEqual(ws.state["research_workbench"], before)
        self.assertEqual(ws.state["extension"], {"preserve": True})
        self.assertEqual(target.read_bytes(), original)

    def test_route_callbacks_use_only_the_supplied_workspace(self):
        first, second = self.make_workspace(), self.make_workspace("two")
        from research_routes import register_research_routes

        for ws in (first, second):
            ws.load(initialize_workflows=True)
        app = FastAPI()
        register_research_routes(app, first, ResearchServices(first, services_for(first)))
        other = FastAPI()
        register_research_routes(other, second, ResearchServices(second, services_for(second)))
        untouched = copy.deepcopy(second.state)
        response = TestClient(app).post(
            "/api/research/brief", json={"purpose": "先行技術調査", "keywords": "独立した条件"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(first.state["research_workbench"]["brief"]["keywords"], "独立した条件")
        self.assertEqual(second.state, untouched)
        self.assertFalse((second.data_dir / "workspace.json").exists())
        saved = json.loads((first.data_dir / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], first.state)

    def test_routes_do_not_import_other_routes_or_receive_globals(self):
        for name in REGISTRATIONS:
            tree = ast.parse((ROOT / (name + "_routes.py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").endswith("_routes"), (name, node.module))
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name.endswith("_routes") for alias in node.names), name)
        app = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        for node in ast.walk(app):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "globals")

    def test_classification_leaf_dependencies_do_not_reintroduce_explorer_cycle(self):
        for name in ("classification_bridge", "classification_symbols", "fi_parser"):
            tree = ast.parse((ROOT / (name + ".py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imports = (
                    [node.module]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                self.assertNotIn("classification_explorer", imports, name)
                if name != "classification_bridge":
                    self.assertNotIn("classification_bridge", imports, name)

    def test_bridge_callbacks_preserve_assigned_and_derived_classification_evidence(self):
        patent = {"id": "JP-fixture", "ipc": ["B60L"], "fi": ["H01M10/0562"], "title": "隔離した分類資料"}
        before = copy.deepcopy(patent)
        enrich = Mock(wraps=classification_symbols.enrich)
        parse = Mock(wraps=classification_symbols.parse_codes)
        lookup = Mock(wraps=classification_symbols.lookup)
        result = classification_bridge.extract(patent, enrich=enrich, parse_codes=parse, lookup=lookup)
        self.assertEqual(result, classification_bridge.extract(patent))
        self.assertTrue(enrich.called and parse.called and lookup.called)
        self.assertEqual(patent, before)
        assigned = next(row for row in result["items"] if row["key"] == "IPC:B60L")
        derived = next(row for row in result["items"] if row["key"] == "IPC:H01M10/0562")
        self.assertEqual(assigned["evidence_status"], "patent_metadata")
        self.assertEqual(derived["evidence_status"], "derived_candidate")

    def test_old_import_paths_reexport_single_implementations(self):
        import convergence_routes
        import convergence_services
        import discovery_routes
        import discovery_services
        import fi_parser
        import research_routes
        import research_services

        for old, new, names in (
            (convergence_routes, convergence_services, ("record_result", "new_observation", "query_matches_theme")),
            (discovery_routes, discovery_services, ("new_discovery", "merge_documents", "public_discovery")),
            (research_routes, research_services, ("new_workbench", "checked_brief", "refined_tree")),
            (explorer, classification_symbols, ("normalize", "parse_codes", "lookup", "enrich")),
            (classification_bridge, fi_parser, ("parse_fi",)),
        ):
            for name in names:
                self.assertIs(getattr(old, name), getattr(new, name))


class OrchestrationSnapshotScopeTests(unittest.TestCase):
    def setUp(self):
        from test_app import module
        import test_orchestration as existing

        self.module = module
        self.client = TestClient(module.app)
        before = module.ws.snapshot(include_settings=True, include_job=True)
        self.addCleanup(module.ws.restore, before)
        self.addCleanup(module.STOP.clear)
        existing.OrchestrationTests.setUp(self)
        self.post = lambda path, body=None: existing.OrchestrationTests.post(self, path, body)
        self.wait_job = lambda: existing.OrchestrationTests.wait_job(self)

    def test_cycle_snapshots_only_the_keys_written_by_each_stage(self):
        module = self.module
        with patch.object(module.ws, "snapshot", wraps=module.ws.snapshot) as snapshot:
            self.post("start", {"judge": False})
            self.post("resume")
            self.post("resume")
        keys = [call.args[0] if call.args else None for call in snapshot.call_args_list]
        self.assertNotIn(None, keys)
        self.assertIn(("orchestration", "selected"), keys)
        self.assertIn(("orchestration",), keys)
        self.assertIn(("candidates", "selected", "queries", "research_workbench"), keys)
        corpus_snapshots = [key for key in keys if "patents" in key]
        self.assertEqual(len(corpus_snapshots), 1)  # Discover alone replaces the shared corpus.
        self.assertEqual(module.STATE["orchestration"]["status"], "waiting_csv")
        self.assertEqual(len(module.STATE["queries"]), 1)

    def test_failed_query_preview_restores_all_its_changes_and_request_state(self):
        module = self.module
        self.post("start", {"judge": False})
        self.post("resume")
        before = copy.deepcopy((module.STATE, module.JOB))
        original = module.make_query

        def failure(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError("隔離したプレビュー失敗")

        with patch.object(module, "make_query", side_effect=failure):
            response = self.client.post("/api/orchestration/resume", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((module.STATE, module.JOB), before)

    def test_failed_final_save_restores_query_and_convergence_but_keeps_error_stage(self):
        module = self.module
        self.post("start", {"judge": False})
        self.post("resume")
        before = copy.deepcopy(module.STATE)
        calls = []
        original = module.save

        def save():
            calls.append(True)
            if len(calls) == 2:  # launch succeeds; finish_query fails; error checkpoint succeeds.
                raise OSError("synthetic disk failure")
            return original()

        with patch.object(module, "save", side_effect=save):
            self.post("resume")
        self.assertEqual(module.JOB["status"], "error")
        self.assertEqual(module.STATE["orchestration"]["status"], "error")
        for key in set(before) | set(module.STATE):
            if key != "orchestration":
                self.assertEqual(module.STATE.get(key), before.get(key), key)
                self.assertEqual(key in module.STATE, key in before, key)


if __name__ == "__main__":
    unittest.main()
