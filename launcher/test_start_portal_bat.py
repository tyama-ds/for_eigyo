"""start_portal.bat の配布形式のテスト（実際の cmd.exe での確認は .github/workflows/launcher.yml）.

LF 改行の bat は、日本語版 Windows の cmd.exe が「chcp 65001」の行を読み違えて即終了する。
「Download ZIP」はリポジトリのバイト列そのままなので、リポジトリ上で CRLF にしておく必要がある。
"""
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
BAT = HERE / "start_portal.bat"


class StartPortalBatTests(unittest.TestCase):
    def test_bat_uses_crlf_line_endings_only(self):
        data = BAT.read_bytes()
        self.assertGreater(data.count(b"\r\n"), 0)
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "改行が LF だけの行がある（CRLF にする）")
        self.assertTrue(data.endswith(b"\r\n"))

    def test_gitattributes_keeps_bat_bytes_unconverted(self):
        rules = (HERE / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("*.bat -text", [line.strip() for line in rules])

    def test_bat_still_runs_launcher_with_its_own_path(self):
        text = BAT.read_bytes().decode("utf-8")
        self.assertIn('set "PORTAL=%~dp0launcher.py"', text)
        self.assertIn('"%PORTAL%" --open %*', text)


if __name__ == "__main__":
    unittest.main()
