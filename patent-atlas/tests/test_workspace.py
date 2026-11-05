"""Workspace ownership and commit contracts, using synthetic temporary data only."""

import copy
import json
import os
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from workspace import Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="patent-atlas-workspace-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = {
            "keywords": "材料",
            "patents": [{"id": "JP-SYNTHETIC-1", "title": "試験資料", "label": "keep", "label_source": "human"}],
            "queries": [{"id": "q1", "boolean_tree": {"op": "text", "value": "材料"}}],
            "discovery": {"patents": [], "status": "planned"},
        }
        self.settings = {
            "provider": "offline",
            "model": "synthetic-model",
            "api_key": "synthetic-api-key",
            "proxy": "http://synthetic-proxy.invalid",
            "ops_key": "synthetic-ops-key",
            "ops_secret": "synthetic-ops-secret",
        }
        self.job = {"id": None, "status": "idle", "progress": 0}
        self.secrets = ("api_key", "proxy", "ops_key", "ops_secret")
        self.ws = Workspace(
            self.root, state=self.state, settings=self.settings, job=self.job, secret_settings=self.secrets
        )

    def test_loading_preserves_aliases_unknown_fields_and_file_bytes(self):
        saved = {
            "state": {"keywords": "過去の条件", "extension": {"future": [1, 2]}},
            "settings": {"model": "preserved-model", "future_setting": True},
        }
        target = self.root / "workspace.json"
        target.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        before = target.read_bytes()
        hook = Mock()
        self.ws.load(after_load=hook)
        hook.assert_called_once_with(self.ws)
        self.assertIs(self.ws.state, self.state)
        self.assertIs(self.ws.settings, self.settings)
        self.assertIs(self.ws.job, self.job)
        self.assertEqual(self.state["keywords"], "過去の条件")
        self.assertEqual(self.state["extension"], {"future": [1, 2]})
        self.assertEqual(self.state["patents"][0]["label"], "keep")
        self.assertEqual(self.settings["model"], "preserved-model")
        self.assertTrue(self.settings["future_setting"])
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.root / "workspace.tmp").exists())

    def test_missing_or_invalid_file_keeps_defaults_and_still_runs_recovery(self):
        before = copy.deepcopy(self.state)
        target = self.root / "workspace.json"
        hook = Mock()
        self.ws.load(after_load=hook)
        self.assertFalse(target.exists())
        self.assertEqual(self.state, before)
        target.write_bytes(b"{incomplete")
        self.ws.load(after_load=hook)
        self.assertEqual(self.state, before)
        self.assertEqual(target.read_bytes(), b"{incomplete")
        self.assertEqual(hook.call_count, 2)

    def test_save_matches_legacy_json_and_never_persists_secrets_or_job(self):
        before = copy.deepcopy(self.settings)
        self.job.update(status="running", progress=43)
        self.ws.save()
        expected = json.dumps(
            {"state": self.state, "settings": {k: v for k, v in self.settings.items() if k not in self.secrets}},
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        target = self.root / "workspace.json"
        self.assertEqual(target.read_text(encoding="utf-8"), expected)
        for key in self.secrets:
            self.assertNotIn(self.settings[key], expected)
        self.assertNotIn("job", json.loads(expected))
        self.assertEqual(self.settings, before)
        self.assertFalse((self.root / "workspace.tmp").exists())

    def test_failed_replace_and_non_finite_values_do_not_overwrite_saved_file(self):
        self.ws.save()
        target = self.root / "workspace.json"
        original = target.read_bytes()
        self.state["keywords"] = "未保存"
        with patch.object(Path, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.ws.save()
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue((self.root / "workspace.tmp").exists())
        self.state["invalid_score"] = float("nan")
        with self.assertRaises(ValueError):
            self.ws.save()
        self.assertEqual(target.read_bytes(), original)

    def test_full_snapshot_restores_roots_without_rebinding_references(self):
        before = copy.deepcopy((self.state, self.settings, self.job))
        snap = self.ws.snapshot(include_settings=True, include_job=True)
        self.state["patents"][0]["label"] = "exclude"
        self.state["new"] = True
        self.state.pop("queries")
        self.settings["api_key"] = "changed"
        self.job.update(status="running", progress=90)
        self.ws.restore(snap)
        self.assertEqual((self.state, self.settings, self.job), before)
        self.assertIs(self.ws.state, self.state)
        self.assertIs(self.ws.settings, self.settings)
        self.assertIs(self.ws.job, self.job)

    def test_partial_snapshot_restores_absence_without_copying_unrelated_patents(self):
        class Uncopiable:
            def __deepcopy__(self, memo):
                raise AssertionError("Unrelated patents must not be copied")

        unrelated = Uncopiable()
        self.state["patents"] = [unrelated]
        original_keys = list(self.state)
        snap = self.ws.snapshot(("keywords", "discovery", "new"))
        self.state["keywords"] = "changed"
        self.state["discovery"]["patents"].append({"id": "new"})
        self.state["new"] = True
        self.state["untouched"] = "later change"
        self.ws.restore(snap)
        self.assertEqual(self.state["keywords"], "材料")
        self.assertEqual(self.state["discovery"]["patents"], [])
        self.assertNotIn("new", self.state)
        self.assertEqual(self.state["untouched"], "later change")
        self.assertIs(self.state["patents"][0], unrelated)
        self.assertEqual(list(self.state), original_keys + ["untouched"])

    def test_transaction_saves_once_on_success(self):
        with patch.object(self.ws, "save", wraps=self.ws.save) as save:
            with self.ws.transaction() as value:
                self.assertIs(value, self.ws)
                self.state["keywords"] = "新しい条件"
            save.assert_called_once_with()
        saved = json.loads((self.root / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], self.state)

    def test_action_failure_restores_state_without_saving(self):
        before = copy.deepcopy(self.state)
        with patch.object(self.ws, "save") as save:
            with self.assertRaisesRegex(ValueError, "synthetic action"):
                with self.ws.transaction():
                    self.state["patents"].clear()
                    self.state["new"] = True
                    raise ValueError("synthetic action")
            save.assert_not_called()
        self.assertEqual(self.state, before)
        self.assertFalse((self.root / "workspace.json").exists())

    def test_save_failure_restores_state_settings_and_job_when_requested(self):
        before = copy.deepcopy((self.state, self.settings, self.job))
        save = Mock(side_effect=OSError("synthetic disk full"))
        with self.assertRaises(OSError):
            with self.ws.transaction(save=save, include_settings=True, include_job=True):
                self.state["queries"].clear()
                self.settings["model"] = "changed"
                self.job["status"] = "running"
        self.assertEqual((self.state, self.settings, self.job), before)
        save.assert_called_once_with()

    def test_pending_file_is_restored_or_removed_only_when_requested(self):
        pending = self.root / "workspace.tmp"
        self.ws.save()
        target = self.root / "workspace.json"
        original_file = target.read_bytes()
        for original_pending in (None, b"previous temporary file"):
            with self.subTest(original_pending=original_pending):
                if original_pending is None:
                    pending.unlink(missing_ok=True)
                else:
                    pending.write_bytes(original_pending)
                before = copy.deepcopy(self.state)
                with patch.object(Path, "replace", side_effect=OSError("synthetic replace failure")):
                    with self.assertRaises(OSError):
                        with self.ws.transaction(restore_pending=True):
                            self.state["keywords"] = "not committed"
                self.assertEqual(self.state, before)
                self.assertEqual(target.read_bytes(), original_file)
                self.assertEqual(pending.read_bytes() if pending.exists() else None, original_pending)

    def test_default_pending_file_policy_keeps_existing_save_failure_behavior(self):
        before = copy.deepcopy(self.state)
        with patch.object(Path, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(OSError):
                with self.ws.transaction():
                    self.state["keywords"] = "not committed"
        self.assertEqual(self.state, before)
        self.assertTrue((self.root / "workspace.tmp").exists())

    def test_running_job_is_rejected_before_mutation_or_snapshot(self):
        self.job["status"] = "running"
        before = copy.deepcopy(self.state)
        with patch.object(self.ws, "snapshot") as snapshot, patch.object(self.ws, "save") as save:
            with self.assertRaises(HTTPException) as raised:
                with self.ws.transaction():
                    self.fail("A running job must not enter a foreground transaction")
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(raised.exception.detail, "処理中です。完了を待つか停止してください。")
            snapshot.assert_not_called()
            save.assert_not_called()
        self.assertEqual(self.state, before)

    def test_background_commit_and_request_guard_do_not_add_saves(self):
        self.job["status"] = "running"
        with patch.object(self.ws, "save", wraps=self.ws.save) as save:
            with self.ws.transaction(check_idle=False, persist=False, include_job=True):
                with self.ws.transaction(check_idle=False):
                    self.state["keywords"] = "background result"
                self.job["progress"] = 100
            save.assert_called_once_with()
        self.assertEqual(self.state["keywords"], "background result")
        self.assertEqual(self.job["progress"], 100)

    def test_transaction_blocks_other_threads_until_commit_and_releases_after_failure(self):
        started, acquired = threading.Event(), threading.Event()

        def contender():
            started.set()
            with self.ws.lock:
                acquired.set()

        worker = threading.Thread(target=contender, daemon=True)
        try:
            with self.assertRaises(ValueError):
                with self.ws.transaction(persist=False):
                    # snapshot/restore and public_state re-enter this same RLock.
                    self.ws.public_state()
                    worker.start()
                    self.assertTrue(started.wait(2))
                    self.assertFalse(acquired.wait(0.05))
                    raise ValueError("synthetic rollback")
            self.assertTrue(acquired.wait(2))
        finally:
            if worker.ident is not None:
                worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_public_state_is_detached_and_retains_secret_presence_flags(self):
        before = copy.deepcopy((self.state, self.settings, self.job))
        result = self.ws.public_state()
        for key in self.secrets:
            self.assertNotIn(key, result["settings"])
            self.assertIs(result["settings"][key + "_set"], True)
            self.assertNotIn(self.settings[key], json.dumps(result))
        result["patents"][0]["label"] = "exclude"
        result["settings"]["model"] = "changed"
        result["job"]["progress"] = 99
        self.assertEqual((self.state, self.settings, self.job), before)

    def test_public_state_observes_unsaved_nested_edits_and_live_job_progress(self):
        first = self.ws.public_state()
        self.state["patents"][0]["label"] = "exclude"
        self.job.update(status="running", progress=20)
        self.settings["api_key"] = ""
        self.ws.invalidate_public_state()  # direct writes outside a transaction invalidate explicitly
        second = self.ws.public_state()
        self.assertEqual(first["patents"][0]["label"], "keep")
        self.assertEqual(second["patents"][0]["label"], "exclude")
        self.assertEqual(second["job"]["progress"], 20)
        self.assertFalse(second["settings"]["api_key_set"])
        self.assertFalse((self.root / "workspace.json").exists())

    def test_domain_projection_can_hide_internal_collections_without_mutation(self):
        before = copy.deepcopy(self.state)

        def project(state):
            return {"keywords": state["keywords"], "document_count": len(state["patents"])}

        result = self.ws.public_state(project)
        self.assertEqual(set(result), {"keywords", "document_count", "settings", "job"})
        self.assertEqual(result["document_count"], 1)
        self.assertEqual(self.state, before)

    def test_app_compatibility_names_reference_the_single_owner(self):
        from test_app import module

        for alias, field in (
            ("STATE", "state"),
            ("SETTINGS", "settings"),
            ("JOB", "job"),
            ("LOCK", "lock"),
            ("DATA", "data_dir"),
        ):
            self.assertIs(getattr(module, alias), getattr(module.ws, field))

    def test_app_reload_keeps_recovery_and_saved_data_contract(self):
        from test_app import module

        saved = {
            "state": {
                **self.state,
                "discovery": {"status": "running", "patents": [{"id": "lab-reference"}]},
                "training": {"mode": "llm", "status": "running", "processed": 10},
                "extension": {"preserve": True},
            },
            "settings": {"model": "preserved-model", "classification_layout": "grid", "llm_timeout": 180},
        }
        target = self.root / "workspace.json"
        target.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        original = target.read_bytes()
        try:
            with patch.dict(os.environ, {"PATENT_ATLAS_DATA": str(self.root)}):
                loaded = runpy.run_path(str(module.ROOT / "app.py"))
            ws = loaded["ws"]
            self.assertIs(ws.state, loaded["STATE"])
            self.assertIs(ws.settings, loaded["SETTINGS"])
            self.assertIs(ws.job, loaded["JOB"])
            self.assertIs(ws.lock, loaded["LOCK"])
            self.assertEqual(ws.state["discovery"]["status"], "stopped")
            self.assertEqual(ws.state["discovery"]["patents"], [{"id": "lab-reference"}])
            self.assertEqual(ws.state["training"]["status"], "cancelled")
            self.assertEqual(ws.state["training"]["processed"], 10)
            self.assertEqual(ws.state["patents"], saved["state"]["patents"])
            self.assertEqual(ws.state["queries"], saved["state"]["queries"])
            self.assertEqual(ws.state["extension"], {"preserve": True})
            self.assertEqual(ws.settings["model"], "preserved-model")
            self.assertEqual(ws.settings["classification_layout"], "grid")
            self.assertEqual(ws.settings["llm_timeout"], 180)
            self.assertEqual(ws.job["status"], "idle")
            self.assertEqual(target.read_bytes(), original)
        finally:
            module.classification_translations.configure_cache(module.DATA)


if __name__ == "__main__":
    unittest.main()
