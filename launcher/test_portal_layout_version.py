"""App Portal: バージョン検出と配置（layout.json）のテスト。アプリは起動しない.

Run with: python -m unittest discover -s launcher -p "test_portal_*.py" -v
"""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "app_portal_layout_under_test", Path(__file__).with_name("launcher.py"))
portal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portal)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class SandboxMixin:
    """<tmp>/for_eigyo/{launcher,progress/projects} と <tmp>/claudecode を模した作業場所。"""

    def make_sandbox(self):
        tmp = tempfile.TemporaryDirectory(prefix="portal sandbox ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        if os.name != "nt":
            # Windows の一時フォルダは 8.3 形式の別名（RUNNER~1 など）で返り、resolve() と表記が食い違う。
            # 他の OS でも同じ状況を作るため、作業場所をシンボリックリンク越しに指す。
            real = self.root / "real"
            real.mkdir()
            (self.root / "alias").symlink_to(real, target_is_directory=True)
            self.root = self.root / "alias"
        self.repo = self.root / "for_eigyo"
        (self.repo / "launcher").mkdir(parents=True)
        values = {
            "BASE": self.repo / "launcher",
            "REPO_ROOT": self.repo,
            "PROGRESS_DIR": self.repo / "progress" / "projects",
            "APPS_FILE": self.repo / "launcher" / "apps.json",
            "LAYOUT_FILE": self.repo / "launcher" / "layout.json",
        }
        for name, value in values.items():
            p = patch.object(portal, name, value)
            p.start()
            self.addCleanup(p.stop)
        git = patch.object(portal, "git_last_updated", return_value="2026-10-01")
        self.git = git.start()
        self.addCleanup(git.stop)
        cache = patch.dict(portal._version_cache, {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)


class VersionDetectionTests(SandboxMixin, unittest.TestCase):
    def setUp(self):
        self.make_sandbox()

    def detect(self, **app):
        return portal.detect_version({"id": "x", **app})

    def test_pyproject_project_version(self):
        write(self.repo / "app1" / "pyproject.toml",
              '[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "app1"\nversion = "0.9.2"\n')
        info = self.detect(cwd="../app1", command=["{python}", "-m", "jupyterlab"])
        self.assertEqual(info, {"version": "0.9.2", "source": "for_eigyo/app1/pyproject.toml",
                                "updated": "2026-10-01"})

    def test_command_package_wins_over_repository_root_pyproject(self):
        cc = self.root / "claudecode"
        (cc / ".git").mkdir(parents=True)
        write(cc / "pyproject.toml", '[project]\nname = "deep-research-tool"\nversion = "1.11.1"\n')
        write(cc / "agent_orchestrator" / "__init__.py", '"""doc"""\n__version__ = "0.1.0"\n')
        write(cc / "deep_research_tool" / "__init__.py", '__version__ = "1.11.1"\n')

        agent = self.detect(cwd="../../claudecode", command=["{python}", "-m", "agent_orchestrator", "serve"])
        self.assertEqual((agent["version"], agent["source"]), ("0.1.0", "claudecode/agent_orchestrator/__init__.py"))
        # cwd がリポジトリ直下なので、日付はリポジトリ全体ではなくパッケージフォルダで取る
        self.assertEqual(Path(self.git.call_args.args[0]), (cc / "agent_orchestrator").resolve())

        research = self.detect(cwd="../../claudecode", command=[
            "{python}", "-c", "from deep_research_tool.webui.server import run_server; run_server(port=8802)"])
        self.assertEqual((research["version"], research["source"]),
                         ("1.11.1", "claudecode/deep_research_tool/__init__.py"))

    def test_src_layout_and_internal_package(self):
        write(self.repo / "fermi" / "src" / "fermiscope" / "__init__.py", '__version__ = "0.1.0"\n')
        info = self.detect(cwd="../fermi", command=["{python}", "-m", "fermiscope.main", "serve"])
        self.assertEqual(info["source"], "for_eigyo/fermi/src/fermiscope/__init__.py")

        write(self.repo / "tens" / "server.py", "print('no version here')\n")
        write(self.repo / "tens" / "tests" / "__init__.py", '__version__ = "9.9.9"\n')
        write(self.repo / "tens" / "tcore" / "__init__.py", '"""core"""\n__version__ = "0.2.0"\n')
        info = self.detect(cwd="../tens", command=["{python}", "server.py"])
        self.assertEqual((info["version"], info["source"]), ("0.2.0", "for_eigyo/tens/tcore/__init__.py"))

    def test_fastapi_version_in_entry_module(self):
        write(self.repo / "atlas" / "run.py", "import app\n")
        write(self.repo / "atlas" / "app" / "__init__.py", "")
        write(self.repo / "atlas" / "app" / "main.py",
              'app = FastAPI(title="Research Atlas", version="2.7.0", description="x")\n')
        info = self.detect(cwd="../atlas", command=["{venv_python}", "run.py"])
        self.assertEqual((info["version"], info["source"]), ("2.7.0", "for_eigyo/atlas/app/main.py"))

    def test_dunder_version_has_priority_over_other_constants(self):
        write(self.repo / "p" / "pkg" / "__init__.py", 'VERSION = "9.0"\n__version__ = "1.2.3"\n')
        self.assertEqual(self.detect(cwd="../p", command=["{python}", "-m", "pkg"])["version"], "1.2.3")

    def test_explicit_version_from(self):
        write(self.repo / "q" / "pyproject.toml", '[project]\nversion = "1.0.0"\n')
        write(self.repo / "q" / "meta" / "release.txt", "")
        write(self.repo / "q" / "VERSION", "2.0.0-beta\n")
        info = self.detect(cwd="../q", command=["{python}", "app.py"], version_from="VERSION")
        self.assertEqual((info["version"], info["source"]), ("2.0.0-beta", "for_eigyo/q/VERSION"))

    def test_progress_file_is_last_resort_and_missing_version_has_no_source(self):
        write(self.repo / "news" / "server.py", "print('hi')\n")
        write(self.repo / "plain" / "server.py", "print('hi')\n")
        write(self.repo / "progress" / "projects" / "news.json", json.dumps({"version": "3.1.0"}))
        write(self.repo / "progress" / "projects" / "plain.json", json.dumps({"name": "plain"}))
        news = self.detect(cwd="../news", command=["{python}", "server.py"])
        self.assertEqual((news["version"], news["source"]), ("3.1.0", "for_eigyo/progress/projects/news.json"))
        plain = self.detect(cwd="../plain", command=["{python}", "server.py"])
        self.assertEqual(plain, {"version": None, "source": None, "updated": "2026-10-01"})

    def test_placeholders_and_dynamic_versions_are_ignored(self):
        write(self.repo / "r" / "pyproject.toml", '[project]\ndynamic = ["version"]\n')
        write(self.repo / "r" / "setup.cfg", "[metadata]\nversion = attr: r.__version__\n")
        write(self.repo / "r" / "package.json", '{"version": "{{ version }}"}')
        self.assertIsNone(self.detect(cwd="../r", command=["{python}", "x.py"])["version"])

    def test_link_only_app_reads_nothing(self):
        self.assertEqual(portal.detect_version({"id": "web", "url": "https://example.com"}),
                         {"version": None, "source": None, "updated": None})
        self.git.assert_not_called()

    def test_results_are_cached_until_fresh_requested(self):
        write(self.repo / "c" / "VERSION", "1.0.0\n")
        app = {"id": "c", "cwd": "../c", "command": ["{python}", "x.py"]}
        self.assertEqual(portal.app_version(app)["version"], "1.0.0")
        write(self.repo / "c" / "VERSION", "1.1.0\n")
        self.assertEqual(portal.app_version(app)["version"], "1.0.0")
        self.assertEqual(portal.app_version(app, fresh=True)["version"], "1.1.0")


class LayoutTests(SandboxMixin, unittest.TestCase):
    def setUp(self):
        self.make_sandbox()

    def test_clean_layout_drops_invalid_values(self):
        layout = portal.clean_layout({
            "groups": [
                {"id": "fav", "name": "  よく使う  ", "apps": ["a", "b", "a", 3, ""]},
                {"id": "../evil", "name": "x", "apps": ["c"]},
                {"id": "fav", "name": "duplicate group", "apps": ["d"]},
                {"id": "_main", "name": "n" * 80, "apps": ["b", "e"]},
                "not a group",
            ],
            "hidden": ["e", "e", None, "f"],
        })
        self.assertEqual(layout, {
            "groups": [
                {"id": "fav", "name": "よく使う", "apps": ["a", "b"]},
                {"id": "_main", "name": "n" * 40, "apps": ["e"]},
            ],
            "hidden": ["e", "f"],
        })
        self.assertEqual(portal.clean_layout("garbage"), {"groups": [], "hidden": []})

    def test_save_and_load_round_trip(self):
        self.assertEqual(portal.load_layout(), {"groups": [], "hidden": []})
        saved = portal.save_layout({"groups": [{"id": "g1", "name": "研究", "apps": ["x"]}], "hidden": ["y"]})
        self.assertEqual(portal.load_layout(), saved)
        self.assertFalse(portal.LAYOUT_FILE.with_name("layout.json.tmp").exists())

    def test_corrupt_file_falls_back_to_default(self):
        write(portal.LAYOUT_FILE, "{not json")
        self.assertEqual(portal.load_layout(), {"groups": [], "hidden": []})


class HttpApiTests(SandboxMixin, unittest.TestCase):
    def setUp(self):
        self.make_sandbox()
        write(self.repo / "app1" / "VERSION", "0.3.0\n")
        write(portal.APPS_FILE, json.dumps({"apps": [
            {"id": "app1", "name": "App 1", "cwd": "../app1", "command": ["{python}", "x.py"],
             "url": "http://127.0.0.1:9", "wait_port": 9},
            {"id": "web", "name": "Web", "url": "https://example.com"},
        ]}))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), portal.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, ctype="application/json"):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     method="GET" if body is None else "POST")
        if body is not None:
            req.add_header("Content-Type", ctype)
        try:
            with self.opener.open(req, timeout=10) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_layout_write_requires_json_content_type(self):
        status, body = self.request("/api/layout", {"groups": [], "hidden": ["x"]}, ctype="text/plain")
        self.assertEqual(status, 415)
        self.assertFalse(body["ok"])
        self.assertFalse(portal.LAYOUT_FILE.exists())

    def test_layout_round_trip_and_apps_payload(self):
        layout = {"groups": [{"id": "g1", "name": "よく使う", "apps": ["web"]}], "hidden": ["app1"]}
        status, body = self.request("/api/layout", layout)
        self.assertEqual((status, body["ok"]), (200, True))

        status, data = self.request("/api/apps")
        self.assertEqual(status, 200)
        self.assertEqual(data["layout"], layout)
        self.assertEqual(data["portal"], {"version": portal.__version__})
        versions = {a["id"]: a["version"] for a in data["apps"]}
        self.assertEqual(versions["app1"]["version"], "0.3.0")
        self.assertEqual(versions["app1"]["source"], "for_eigyo/app1/VERSION")
        self.assertIsNone(versions["web"]["version"])

        status, fresh = self.request("/api/apps?fresh=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(fresh["apps"]), 2)

    def test_add_app_keeps_optional_version_file(self):
        status, body = self.request("/api/apps", {
            "name": "Mine", "command": "{python} app.py", "cwd": "../app1", "port": "8999",
            "url": "http://127.0.0.1:8999", "version_from": "VERSION"})
        self.assertEqual((status, body["ok"]), (200, True))
        saved = json.loads(portal.APPS_FILE.read_text(encoding="utf-8"))["apps"][-1]
        self.assertEqual(saved["version_from"], "VERSION")
        self.assertEqual(body["app"]["version"]["version"], "0.3.0")


if __name__ == "__main__":
    unittest.main()
