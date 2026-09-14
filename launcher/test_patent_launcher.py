"""Checks for app-specific Python resolution; no app processes are started."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import launcher


class AppPythonTests(unittest.TestCase):
    def test_platform_venv_and_existing_python_placeholder(self):
        for platform, relative in (("nt", "Scripts/python.exe"), ("posix", "bin/python")):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory(prefix="app with spaces ") as directory:
                cwd = Path(directory)
                expected = cwd / ".venv" / relative
                expected.parent.mkdir(parents=True)
                expected.touch()
                command = ["{app_python}", "run.py", "--port", "8810", "{python}"]
                with patch.object(launcher.os, "name", platform):
                    resolved = launcher.resolve_command(command, cwd)
                self.assertEqual(resolved, [str(expected), "run.py", "--port", "8810", sys.executable])
                self.assertEqual(command[0], "{app_python}")

    def test_missing_venv_falls_back_to_portal_python(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                launcher.resolve_command(["{app_python}", "run.py"], Path(directory)),
                [sys.executable, "run.py"],
            )

    def test_registry_ports_and_patent_entry(self):
        apps = json.loads(launcher.APPS_FILE.read_text(encoding="utf-8"))["apps"]
        app = next(item for item in apps if item["id"] == "patent-atlas")
        self.assertEqual(app["cwd"], "../patent-atlas")
        self.assertEqual(app["command"], ["{app_python}", "run.py", "--port", "8810"])
        self.assertEqual(app["url"], "http://127.0.0.1:8810")
        self.assertEqual(app["wait_port"], 8810)
        self.assertFalse(any(item["wait_port"] == 8810 for item in apps if item["id"] != "patent-atlas"))


if __name__ == "__main__":
    unittest.main()
