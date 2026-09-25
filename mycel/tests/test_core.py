"""リンク解析・Vault・インデックス・アプリ操作・プラグインのテスト（標準ライブラリのみ）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from mycelcore import links as L  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.plugins import PluginVeto  # noqa: E402
from mycelcore.vault import ConflictError, Vault, VaultError, normalize_rel  # noqa: E402


class LinksTest(unittest.TestCase):
    def test_parse_wikilink(self):
        self.assertEqual(L.parse_wikilink("ノート#見出し|別名"), ("ノート", "見出し", "別名"))
        self.assertEqual(L.parse_wikilink(" フォルダ/名前 "), ("フォルダ/名前", "", ""))

    def test_extract_links_skips_code(self):
        text = "[[A]] と `[[B]]`\n```\n[[C]]\n```\n[[D|でぃー]] [[A#x]]"
        self.assertEqual(L.extract_links(text), ["A", "D", "A"])

    def test_tags(self):
        text = "---\ntags: 顧客, 重要\n---\n# 見出し\n本文 #提案中 と #2026 と https://x.jp/#frag\n```\n#コード\n```"
        self.assertEqual(L.extract_tags(text), ["顧客", "重要", "提案中"])

    def test_frontmatter_and_headings(self):
        text = "---\n顧客: A社\n確度: 60%\n---\n# 題\n## 背景\n```\n# no\n```"
        props, body, start = L.split_frontmatter(text)
        self.assertEqual(props, {"顧客": "A社", "確度": "60%"})
        self.assertEqual(start, 4)
        self.assertEqual([h["text"] for h in L.extract_headings(text)], ["題", "背景"])

    def test_rewrite_links_keeps_heading_and_alias(self):
        text = "[[旧]] [[旧#節|別名]] [[旧名ではない]]\n```\n[[旧]]\n```"
        out, n = L.rewrite_links(text, "旧", "新")
        self.assertEqual(n, 2)
        self.assertEqual(out, "[[新]] [[新#節|別名]] [[旧名ではない]]\n```\n[[旧]]\n```")

    def test_chunk_note(self):
        text = "# 題\n前文\n## A\n" + ("あ" * 2000) + "\n## B\n短い"
        chunks = L.chunk_note(text, max_chars=500)
        self.assertEqual(chunks[0]["heading"], "題")
        self.assertTrue(all(len(c["text"]) <= 760 for c in chunks))
        self.assertEqual(chunks[-1]["heading"], "B")


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.v = Vault(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_and_reject(self):
        self.assertEqual(normalize_rel("a/b"), "a/b.md")
        for bad in ("../x", ".mycel/index", "a/../../b", "", "a:b", "a/ b", "a/.hidden"):
            with self.assertRaises(VaultError, msg=bad):
                normalize_rel(bad)

    def test_write_conflict(self):
        v1 = self.v.write("n", "1")
        v2 = self.v.write("n", "2", base_version=v1)
        with self.assertRaises(ConflictError) as cm:
            self.v.write("n", "3", base_version=v1)
        self.assertEqual(cm.exception.current_text, "2")
        self.assertEqual(cm.exception.current_version, v2)

    def test_list_skips_internal_and_delete_to_trash(self):
        self.v.write("a/b", "x")
        (self.v.internal / "junk.md").write_text("x", encoding="utf-8")
        self.assertEqual([f[0] for f in self.v.list_files()], ["a/b.md"])
        where = self.v.delete("a/b")
        self.assertTrue((self.v.root / where).is_file())
        self.assertFalse((self.v.root / "a").exists())


RECORDER_SRC = """
from mycelcore.plugins import Plugin, PluginVeto


class Recorder(Plugin):
    def setup(self, ctx):
        super().setup(ctx)
        self.events = []

    def before_save(self, event):
        if "禁止" in (event.text or ""):
            raise PluginVeto("この内容は保存できません")

    def on_saved(self, event):
        self.events.append(("saved", event.path, event.author))

    def on_created(self, event):
        self.events.append(("created", event.path, event.author))

    def on_renamed(self, event):
        self.events.append(("renamed", event.path, event.old_path))

    def routes(self):
        return {"count": lambda p: {"n": len(self.events)}}
"""


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.plugdir = root / "plugins"
        self.plugdir.mkdir()
        (self.plugdir / "recorder.py").write_text(RECORDER_SRC, encoding="utf-8")
        cfg = root / "cfg.json"
        cfg.write_text('{"user_name": "tester", "plugins": ["recorder", "missing"]}', encoding="utf-8")
        self.app = MycelApp(config_path=cfg, vault_override=str(root / "vault"), plugin_dir=self.plugdir)
        self.events = self.app.plugins.loaded["recorder"].events

    def tearDown(self):
        self.app.plugins.unload()
        self.app.index.close()
        self.tmp.cleanup()

    def test_sample_vault_seeded_and_links(self):
        n = self.app.note("顧客/A社 生産管理システム更改")
        self.assertEqual(n["props"]["顧客"], "A社")
        back = {b["path"] for b in n["backlinks"]}
        self.assertIn("人物/田中部長（A社）.md", back)
        self.assertEqual([u["path"] for u in n["unlinked"]], ["顧客/B社 物流DX.md"])
        unresolved = [o for o in n["outgoing"] if o["path"] is None]
        self.assertEqual([o["target"] for o in unresolved], ["競合比較表"])

    def test_search_japanese(self):
        self.assertEqual(self.app.index.search("稼働率")[0]["path"], "人物/田中部長（A社）.md")
        self.assertTrue(self.app.index.search("在庫 拠点"))
        self.assertTrue(self.app.index.search("A社"))  # 3 文字未満は LIKE
        self.assertEqual(self.app.index.search("存在しない語句"), [])

    def test_save_create_and_plugin_events(self):
        r = self.app.create(title="新規", folder="顧客")
        self.assertEqual(r["path"], "顧客/新規.md")
        s = self.app.save(r["path"], "# 新規\n[[提案の型]]", r["version"])
        self.assertNotEqual(s["version"], r["version"])
        self.assertIn("顧客/新規.md", {b["path"] for b in self.app.index.backlinks("ナレッジ/提案の型.md")})
        self.assertIn(("created", "顧客/新規.md", "tester"), self.events)
        self.assertIn(("saved", "顧客/新規.md", "tester"), self.events)
        self.assertIn("missing", self.app.plugins.errors)
        self.assertEqual(self.app.plugins.route("recorder", "count")({})["n"], len(self.events))

    def test_plugin_veto(self):
        with self.assertRaises(PluginVeto):
            self.app.save("ホーム", "禁止ワード", None)
        self.assertNotIn("禁止", self.app.vault.read("ホーム")[0])

    def test_conflict_via_app(self):
        n = self.app.note("ホーム")
        (self.app.vault.root / "ホーム.md").write_text("外部で変更", encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.app.save("ホーム", "こちらの変更", n["version"])

    def test_rename_updates_links(self):
        r = self.app.rename("人物/田中部長（A社）.md", "人物/田中本部長（A社）")
        self.assertEqual(r["path"], "人物/田中本部長（A社）.md")
        self.assertIn("日報/2026-09-18.md", r["updated"])
        self.assertIn("[[田中本部長（A社）]]", self.app.vault.read("日報/2026-09-18.md")[0])
        self.assertEqual(len(self.app.index.backlinks(r["path"])), 2)
        self.assertIn(("renamed", r["path"], "人物/田中部長（A社）.md"), self.events)

    def test_rename_to_duplicate_title_uses_path_links(self):
        self.app.create("ナレッジ/ホーム", text="# 別のホーム")
        self.app.create("メモ/参照", text="[[提案の型]]")
        r = self.app.rename("ナレッジ/提案の型.md", "ナレッジ/ホーム2")
        self.assertIn("[[ホーム2]]", self.app.vault.read("メモ/参照")[0])
        # 既存と同名になる場合はパスで書く
        self.app.create("メモ/参照2", text="[[ホーム2]]")
        r = self.app.rename(r["path"], "アーカイブ/ホーム")
        self.assertIn("[[アーカイブ/ホーム]]", self.app.vault.read("メモ/参照2")[0])
        self.assertEqual(self.app.index.resolve("アーカイブ/ホーム"), "アーカイブ/ホーム.md")

    def test_daily_and_template(self):
        d = self.app.daily("2030-01-02")
        self.assertTrue(d["created"])
        text = self.app.vault.read(d["path"])[0]
        self.assertTrue(text.startswith("# 2030-01-02"))
        self.assertFalse(self.app.daily("2030-01-02")["created"])
        r = self.app.create(title="B社 定例", folder="顧客", template="商談メモ")
        body = self.app.vault.read(r["path"])[0]
        self.assertIn("# B社 定例", body)
        self.assertIn("記入: tester", body)

    def test_graph_local(self):
        g = self.app.index.graph("ナレッジ/提案の型.md", 1)
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("顧客/A社 生産管理システム更改.md", ids)
        self.assertNotIn("人物/田中部長（A社）.md", ids)
        full = self.app.index.graph()
        self.assertTrue(any(not n["exists"] for n in full["nodes"]))

    def test_external_edit_picked_up_by_sync(self):
        p = self.app.vault.root / "外部.md"
        p.write_text("# 外部\n[[ホーム]]", encoding="utf-8")
        self.app.index.sync(force=True)
        self.assertIn("外部.md", {b["path"] for b in self.app.index.backlinks("ホーム.md")})
        p.unlink()
        self.app.index.sync(force=True)
        self.assertIsNone(self.app.index.get("外部.md"))

    def test_delete(self):
        self.app.delete("ホーム")
        self.assertIsNone(self.app.index.get("ホーム.md"))
        with self.assertRaises(VaultError):
            self.app.note("ホーム")


if __name__ == "__main__":
    unittest.main()
