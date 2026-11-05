"""Public-state caching preserves the detached API and observes unsaved writes."""

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from workspace import Workspace


class PublicStateCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="patent-atlas-cache-")
        self.addCleanup(temp.cleanup)
        self.ws = Workspace(
            temp.name,
            state={"patents": [{"id": "JP1", "label": "keep"}], "selected": [], "queries": []},
            settings={"model": "local", "api_key": "synthetic-secret"},
            job={"status": "idle", "progress": 0},
            secret_settings=("api_key",),
        )
        self.project = Mock(side_effect=lambda state: state)

    def test_unchanged_poll_reuses_bytes_without_projection_or_deepcopy(self):
        first = self.ws.public_state_json(self.project)
        with patch("workspace.copy.deepcopy", side_effect=AssertionError("cache hit must not copy")):
            second = self.ws.public_state_json(self.project)
        self.assertIs(first, second)
        self.project.assert_called_once()
        self.assertNotIn(b"synthetic-secret", first)
        self.assertTrue(json.loads(first)["settings"]["api_key_set"])

    def test_dictionary_return_cannot_mutate_state_or_cached_payload(self):
        first = self.ws.public_state(self.project)
        expected = self.ws.public_state_json(self.project)
        first["patents"][0]["label"] = "exclude"
        first["settings"]["model"] = "changed"
        first["job"]["progress"] = 99
        self.assertEqual(self.ws.public_state_json(self.project), expected)
        self.assertEqual(self.ws.public_state(self.project)["patents"][0]["label"], "keep")
        self.assertEqual(self.ws.state["patents"][0]["label"], "keep")
        self.project.assert_called_once()

    def test_unsaved_nested_mutations_and_progress_are_visible(self):
        self.ws.public_state_json(self.project)
        changes = (
            lambda: self.ws.state["patents"][0].update(label="exclude"),
            lambda: self.ws.state["patents"][0].update(ipc=["B60L"]),
            lambda: self.ws.state["selected"].append("IPC:B60L"),
            lambda: self.ws.state["queries"].append({"id": "q1"}),
            lambda: self.ws.state.pop("queries"),
            lambda: self.ws.settings.update(model="another-local-model"),
            lambda: self.ws.settings.update(api_key=""),
            lambda: self.ws.job.update(status="running", progress=42),
        )
        for index, change in enumerate(changes, 2):
            change()
            actual = json.loads(self.ws.public_state_json(self.project))
            self.assertEqual(actual["patents"], self.ws.state["patents"])
            self.assertEqual(actual["selected"], self.ws.state["selected"])
            self.assertEqual(actual["job"], self.ws.job)
            self.assertEqual(actual["settings"]["model"], self.ws.settings["model"])
            self.assertEqual(actual["settings"]["api_key_set"], bool(self.ws.settings["api_key"]))
            self.assertEqual(self.project.call_count, index)
        self.assertFalse((self.ws.data_dir / "workspace.json").exists())

    def test_failed_transaction_cannot_publish_rolled_back_projection(self):
        original = self.ws.public_state_json(self.project)
        with self.assertRaises(OSError):
            with self.ws.transaction(save=Mock(side_effect=OSError("synthetic save failure"))):
                self.ws.state["patents"][0]["label"] = "exclude"
                self.assertNotEqual(self.ws.public_state_json(self.project), original)
        self.assertEqual(self.ws.public_state_json(self.project), original)

    def test_successful_transaction_and_reload_refresh_cached_projection(self):
        self.ws.public_state_json(self.project)
        with self.ws.transaction():
            self.ws.state["patents"][0]["label"] = "exclude"
        self.assertEqual(json.loads(self.ws.public_state_json(self.project))["patents"][0]["label"], "exclude")
        self.ws.state["patents"].clear()
        self.ws.public_state_json(self.project)
        self.ws.load()
        self.assertEqual(len(json.loads(self.ws.public_state_json(self.project))["patents"]), 1)

    def test_projection_and_external_dependency_are_part_of_cache_identity(self):
        revision = [0]
        caption = ["旧名称"]
        project = Mock(side_effect=lambda state: {"caption": caption[0]})
        token = lambda: revision[0]
        first = self.ws.public_state_json(project, cache_token=token)
        self.ws.public_state_json(project, cache_token=token)
        project.assert_called_once()
        caption[0], revision[0] = "更新名称", 1
        self.assertNotEqual(self.ws.public_state_json(project, cache_token=token), first)
        self.assertEqual(json.loads(self.ws.public_state_json())["patents"], self.ws.state["patents"])

    def test_non_json_values_preserve_dict_and_fastapi_encoding_behavior(self):
        self.ws.state["timestamp"] = datetime(2026, 11, 5, 12, 0)
        first = self.ws.public_state(self.project)
        self.assertIsInstance(first["timestamp"], datetime)
        self.assertEqual(self.ws.public_state_json(self.project), JSONResponse(jsonable_encoder(first)).body)
        self.ws.state["timestamp"] = datetime(2026, 11, 6, 12, 0)
        self.assertEqual(json.loads(self.ws.public_state_json(self.project))["timestamp"], "2026-11-06T12:00:00")

    def test_non_finite_json_is_still_rejected(self):
        self.ws.public_state_json(self.project)
        self.ws.state["patents"][0]["score"] = float("nan")
        with self.assertRaises(ValueError):
            self.ws.public_state_json(self.project)

    def test_cache_is_not_persisted_and_workspaces_do_not_share_it(self):
        self.ws.public_state_json(self.project)
        self.ws.save()
        saved = json.loads((self.ws.data_dir / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"state", "settings"})
        self.assertEqual(saved["state"], self.ws.state)
        self.assertEqual(saved["settings"], {"model": "local"})
        other = Workspace(self.ws.data_dir / "other", state={"patents": []}, settings={}, job={"status": "idle"})
        self.assertEqual(json.loads(other.public_state_json())["patents"], [])
        self.assertEqual(len(json.loads(self.ws.public_state_json(self.project))["patents"]), 1)


class PublicStateApiCacheTests(unittest.TestCase):
    def setUp(self):
        from test_app import module

        self.module = module
        self.client = TestClient(module.app)
        self.assertNotEqual(module.JOB["status"], "running")
        snapshot = module.ws.snapshot(include_settings=True, include_job=True)
        self.addCleanup(module.ws.restore, snapshot)
        module.JOB["status"] = "idle"
        self.assertEqual(self.client.post("/api/demo").status_code, 200)
        module.ws.invalidate_public_state()

    def test_get_state_keeps_json_bytes_and_skips_repeated_projection(self):
        module = self.module
        expected = JSONResponse(jsonable_encoder(module.public_state())).body
        with patch.object(module, "_public_workspace", wraps=module._public_workspace) as project:
            first = self.client.get("/api/state")
            with patch("workspace.copy.deepcopy", side_effect=AssertionError("unchanged GET must not copy")):
                second = self.client.get("/api/state")
            self.assertEqual(first.content, expected)
            self.assertEqual(second.content, expected)
            self.assertEqual(first.headers["content-type"], "application/json")
            self.assertEqual(first.headers["content-length"], str(len(expected)))
            project.assert_called_once()
            module.progress(42, "判定中")
            updated = self.client.get("/api/state").json()
            self.assertEqual(updated["job"]["progress"], 42)
            self.assertEqual(updated["job"]["message"], "判定中")
            self.assertEqual(project.call_count, 2)

    def test_caption_cache_reload_updates_display_without_rewriting_workspace(self):
        module = self.module
        translations = module.classification_translations
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / "workspace.json").read_bytes()
        self.client.get("/api/state")
        code = next(c["code"] for c in module.STATE["candidates"] if c["kind"] == "IPC")
        temp = tempfile.TemporaryDirectory(prefix="patent-atlas-caption-cache-")
        self.addCleanup(temp.cleanup)
        self.addCleanup(translations.configure_cache, module.DATA)
        row = dict(
            code=code,
            title_ja="隔離した更新名称",
            title_ja_status="official_translation",
            title_ja_source=translations.BASE + "test.html",
            title_ja_version="fixture",
        )
        (Path(temp.name) / "ipc-japanese-captions.json").write_text(json.dumps({code: row}), encoding="utf-8")
        token = translations.cache_token()
        translations.configure_cache(temp.name)
        self.assertNotEqual(translations.cache_token(), token)
        result = self.client.get("/api/state").json()
        candidate = next(c for c in result["candidates"] if c["code"] == code and c["kind"] == "IPC")
        self.assertEqual(candidate["title_ja"], row["title_ja"])
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / "workspace.json").read_bytes(), disk)


if __name__ == "__main__":
    unittest.main()
