"""Run with: python -m unittest discover -s launcher -p test_launcher.py -v."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("app_portal_under_test", Path(__file__).with_name("launcher.py"))
portal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portal)


class InterpreterSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portal test ")
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.app = {
            "id": "isolated-app",
            "cwd": str(self.cwd),
            "command": ["{venv_python}", "run.py", "--no-browser", "--port", "8778"],
        }

    def create_python(self, platform):
        path = self.cwd / ".venv" / ("Scripts/python.exe" if platform == "nt" else "bin/python")
        path.parent.mkdir(parents=True)
        path.touch()
        return path

    def test_existing_python_token_does_not_require_app_venv(self):
        app = {"command": ["{python}", "-m", "existing_app", "literal argument"]}
        self.assertEqual(portal.resolve_command(app["command"], self.cwd), [sys.executable, "-m", "existing_app", "literal argument"])

    def test_windows_venv_path_preserves_spaces_and_arguments(self):
        executable = self.create_python("nt")
        with patch.object(portal.os, "name", "nt"):
            command = portal.resolve_command(self.app["command"], self.cwd)
        self.assertEqual(command, [str(executable), *self.app["command"][1:]])

    def test_posix_venv_path(self):
        executable = self.create_python("posix")
        with patch.object(portal.os, "name", "posix"):
            command = portal.resolve_command(self.app["command"], self.cwd)
        self.assertEqual(command, [str(executable), *self.app["command"][1:]])

    def test_missing_venv_has_actionable_setup_error(self):
        with self.assertRaisesRegex(FileNotFoundError, "README.*\\.venv"):
            portal.resolve_command(self.app["command"], self.cwd)

    def test_missing_venv_never_spawns_or_registers_process(self):
        with patch.object(portal, "app_state", return_value="stopped"), \
             patch.dict(portal._procs, {}, clear=True), \
             patch.object(portal.subprocess, "Popen") as spawn:
            result = portal.launch_app(self.app)
            self.assertFalse(result["ok"])
            self.assertIn("README", result["error"])
            spawn.assert_not_called()
            self.assertNotIn(self.app["id"], portal._procs)

    def test_server_process_is_started_and_managed_directly(self):
        executable = self.create_python(os.name)
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(portal, "app_state", return_value="stopped"), \
             patch.dict(portal._procs, {}, clear=True), \
             patch.object(portal.subprocess, "Popen", return_value=process) as spawn, \
             patch.object(portal.threading, "Thread"):
            result = portal.launch_app(self.app)
            self.assertTrue(result["ok"])
            # launch_app resolves cwd; Windows temp paths can use 8.3 aliases.
            self.assertEqual(spawn.call_args.args[0], [str(executable.resolve()), "run.py", "--no-browser", "--port", "8778"])
            self.assertEqual(spawn.call_args.kwargs["cwd"], str(self.cwd.resolve()))
            self.assertIs(portal._procs[self.app["id"]]["proc"], process)


if __name__ == "__main__":
    unittest.main()
