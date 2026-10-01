"""人物・組織（文書に出てくる人と会社で文書をつなぐ）のテスト。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

import server  # noqa: E402
from mock_llm import start_mock_llm  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.entities import EntityStore, extract_rule, org_key, person_key, strip_person  # noqa: E402
from mycelcore.vault import VaultError  # noqa: E402
from samples import make_eml  # noqa: E402


def keys(items, kind):
    return {i["key"] for i in items if i["type"] == kind}


class RuleTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(person_key("田中部長（A社）"), "田中")
        self.assertEqual(person_key("鈴木 一郎 様"), "鈴木一郎")
        self.assertEqual(strip_person("佐藤課長"), ("佐藤", "課長"))
        self.assertEqual(org_key("株式会社テックワン"), org_key("テックワン（株）"))
        self.assertEqual(org_key("A社"), org_key("株式会社A"))

    def test_extract(self):
        text = ("---\n顧客: A社\n---\n# 打合せ\n- 差出人: 田中 太郎 <t@a.co.jp>\n"
                "出席者：B社 佐藤課長、鈴木、株式会社テックワン 高橋様\n"
                "[[田中部長（A社）]]：決裁者。製造本部長の意向。皆様、お客様、競合他社、親会社、東京本社\n"
                "```\n山本様（コードの中）\n```\n")
        items = extract_rule(text)
        self.assertEqual(keys(items, "person"), {"田中太郎", "佐藤", "鈴木", "高橋", "田中"})
        self.assertEqual(keys(items, "org"), {"a", "b", "テックワン"})
        tanaka = next(i for i in items if i["key"] == "田中")
        self.assertEqual((tanaka["title"], tanaka["org"], tanaka["role"]), ("部長", "A社", "決裁者"))
        sato = next(i for i in items if i["key"] == "佐藤")
        self.assertEqual((sato["org"], sato["role"]), ("B社", "出席者"))
        self.assertEqual(next(i for i in items if i["key"] == "田中太郎")["role"], "差出人")
        self.assertEqual(next(i for i in items if i["key"] == "a")["role"], "顧客")

    def test_canon_merges_surname_with_full_name_in_same_org(self):
        with tempfile.TemporaryDirectory() as t:
            st = EntityStore(Path(t))
            st.put("a.md", [{"type": "person", "key": "田中", "name": "田中部長", "org": "A社"}], "1", "rule")
            st.put("b.md", [{"type": "person", "key": "田中太郎", "name": "田中 太郎", "org": "A社"}], "1", "rule")
            st.put("c.md", [{"type": "person", "key": "田中花子", "name": "田中花子", "org": "B社"}], "1", "rule")
            ents = st.model()["ents"]
            self.assertIn(("person", "田中太郎"), ents)
            self.assertNotIn(("person", "田中"), ents)
            self.assertEqual(set(ents[("person", "田中太郎")]["paths"]), {"a.md", "b.md"})
            self.assertEqual(ents[("person", "田中太郎")]["display"], "田中 太郎")
            st.close()


class PeopleAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.llm, self.llm_url, self.reqs = start_mock_llm()
        v = root / "vault"
        (v / "資料").mkdir(parents=True)
        make_eml(v / "資料" / "返信.eml")
        (v / "資料" / "議事録.txt").write_text("B社 定例\n出席者：B社 山本さん、田中部長\nB社の山本さんが窓口。物流倉庫の自動化を検討。",
                                             encoding="utf-8")
        (v / "資料" / "倉庫自動化の概要.txt").write_text("物流倉庫の自動化 提案の骨子。B社向け。", encoding="utf-8")
        self.app = MycelApp(config_path=root / "cfg.json", vault_override=str(v), initial_load="sync",
                            seed_sample=False)
        # サンプルの人物ノートと案件ノート
        self.app.create("人物/田中部長（A社）", text="# 田中部長（A社）\n製造本部長。\n")
        self.app.create("顧客/A社 更改", text="---\n顧客: A社\n---\n# A社 更改\n- [[田中部長（A社）]]：決裁者\n- 鈴木さん（A社 情報システム）：窓口\n")
        self.app.create("日報/0918", text="# 0918\n- 出席：[[田中部長（A社）]]、鈴木さん\n- 田中部長「ラインを止めたくない」\n")

    def tearDown(self):
        self.llm.shutdown()
        self.app.plugins.unload()
        self.app.index.close()
        self.app.entities.close()
        self.tmp.cleanup()

    def test_person_detail_without_links(self):
        d = self.app.people.detail("person", "田中")
        paths = {a["path"] for a in d["appearances"]}
        self.assertTrue({"顧客/A社 更改.md", "日報/0918.md", "資料/議事録.txt", "人物/田中部長（A社）.md"} <= paths)
        roles = {r["name"] for r in d["roles"]}
        self.assertTrue({"決裁者", "出席者"} <= roles)
        self.assertEqual(d["affiliations"][0]["name"], "A社")
        self.assertEqual(d["note"], "人物/田中部長（A社）.md")
        self.assertIn("鈴木", {p["key"] for p in d["co_people"]})
        self.assertIn("山本", {p["key"] for p in d["co_people"]})              # 議事録で一緒に出てくる
        # 名前は出てこないが関係がありそうな文書（推定）
        self.assertIn("資料/倉庫自動化の概要.txt", {r["path"] for r in d["related"]})
        org = self.app.people.detail("org", "a")
        self.assertIn("田中", {m["key"] for m in org["members"]})
        lst = self.app.people.list("person")
        self.assertEqual(lst[0]["key"], "田中")
        self.assertEqual(lst[0]["org"], "A社")

    def test_entities_of_doc_and_graph(self):
        of = self.app.people.of("資料/議事録.txt")
        self.assertEqual({(e["type"], e["key"]) for e in of} >= {("person", "山本"), ("person", "田中"), ("org", "b")}, True)
        yam = next(e for e in of if e["key"] == "山本")
        self.assertEqual(yam["roles"], ["出席者"])
        g = self.app.graph(None, people=True)
        self.assertIn("~p:田中", {n["id"] for n in g["nodes"]})
        self.assertIn(["資料/議事録.txt", "~p:田中", "ent"], g["edges"])
        local = self.app.graph("資料/議事録.txt", 2, people=True)
        self.assertIn("日報/0918.md", {n["id"] for n in local["nodes"]})        # 田中部長を介してつながる
        plain = self.app.graph("資料/議事録.txt", 2)
        self.assertNotIn("日報/0918.md", {n["id"] for n in plain["nodes"]})

    def test_corrections_hide_merge_ignore_add(self):
        p = "資料/議事録.txt"
        # この文書から外す → 抽出し直しても外したまま → 戻す
        self.app.entities.hide(p, "person", "山本")
        self.assertNotIn("山本", keys(self.app.people.of(p), "person"))
        self.app.people.sync(force=True)
        (self.app.vault.root / p).write_text("B社 定例\n出席者：B社 山本さん、田中部長\n追記", encoding="utf-8")
        self.app.update_index(None, wait=True)
        self.assertNotIn("山本", keys(self.app.people.of(p), "person"))
        self.app.entities.unhide(p, "person", "山本")
        self.assertIn("山本", keys(self.app.people.of(p), "person"))
        # 同一人物としてまとめる → 解除
        self.app.people.merge("person", "鈴木", "田中")
        d = self.app.people.detail("person", "田中")
        self.assertIn({"key": "鈴木", "user": True}, d["aliases"])
        with self.assertRaises(VaultError):
            self.app.people.detail("person", "鈴木")
        self.app.people.unmerge("person", "鈴木")
        self.assertTrue(self.app.people.detail("person", "鈴木")["appearances"])
        # 人名ではない → 戻す
        self.app.people.ignore("person", "山本")
        self.assertNotIn("山本", keys(self.app.people.list("person"), "person"))
        self.assertIn({"type": "person", "key": "山本"}, self.app.people.ignored())
        self.app.people.unmerge("person", "山本")
        self.assertIn("山本", keys(self.app.people.list("person"), "person"))
        # 手で足す → 外す
        r = self.app.people.add("資料/倉庫自動化の概要.txt", "person", "山本さん（B社）", "提案先")
        yam = next(e for e in r["entities"] if e["key"] == "山本")
        self.assertTrue(yam["manual"])
        self.assertEqual(yam["roles"], ["提案先"])
        self.app.entities.hide("資料/倉庫自動化の概要.txt", "person", "山本")
        self.assertNotIn("山本", keys(self.app.people.of("資料/倉庫自動化の概要.txt"), "person"))

    def test_move_keeps_ai_results(self):
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock"})
        self.app.extract_people(["資料"])
        st = self.app.jobs.wait(20)
        self.assertEqual(st["state"], "done", st)
        p = "資料/議事録.txt"
        of = {e["key"]: e for e in self.app.people.of(p)}
        self.assertEqual(of["山本一郎"]["method"], "llm")                       # AI の結果（フルネーム）
        self.assertEqual(self.app.people.status()["ai_extracted"], 3)
        # 「山本さん」（ルール）は同じ B社 の「山本 一郎」（AI）にまとまる
        self.assertIn("資料/議事録.txt", {a["path"] for a in self.app.people.detail("person", "山本一郎")["appearances"]})
        self.app.move_item(p, "資料/2026/議事録.txt")
        of2 = {e["key"]: e for e in self.app.people.of("資料/2026/議事録.txt")}
        self.assertEqual(of2["山本一郎"]["method"], "llm")
        self.assertEqual(self.app.people.of(p), [])

    def test_profile(self):
        r = self.app.people.profile("person", "田中")
        self.assertFalse(r["llm"])
        self.assertTrue(any("A社" in f for f in r["facts"]))
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock"})
        r = self.app.people.profile("person", "田中")
        self.assertTrue(r["llm"])
        self.assertIn("## 所属と立場", r["answer"])
        self.assertTrue(r["sources"])
        prompt = [q for q in self.reqs if "[TASK:profile]" in q["body"]["messages"][-1]["content"]][-1]
        self.assertIn("一緒に出てくる人", prompt["body"]["messages"][-1]["content"])


class PeopleApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"), initial_load="sync")
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.plugins.unload()
        cls.app.index.close()
        cls.tmp.cleanup()

    def call(self, method, path, obj=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=json.dumps(obj).encode() if obj is not None else None,
                     headers={"Content-Type": "application/json"} if obj is not None else {})
        res = conn.getresponse()
        body = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, body

    def test_flow(self):
        st, r = self.call("GET", "/api/people?type=person")
        self.assertEqual(st, 200)
        self.assertIn("田中", [e["key"] for e in r["entities"]])                 # サンプル Vault から
        st, d = self.call("GET", "/api/people/entity?type=person&key=" + quote("田中"))
        self.assertEqual(st, 200, d)
        self.assertTrue(d["appearances"])
        path = d["appearances"][0]["path"]
        st, of = self.call("GET", "/api/people/of?path=" + quote(path))
        self.assertIn("田中", [e["key"] for e in of["entities"]])
        st, r = self.call("POST", "/api/people/hide", {"path": path, "type": "person", "key": "田中"})
        self.assertNotIn("田中", [e["key"] for e in r["entities"]])
        st, r = self.call("POST", "/api/people/unhide", {"path": path, "type": "person", "key": "田中"})
        self.assertIn("田中", [e["key"] for e in r["entities"]])
        st, r = self.call("POST", "/api/people/add", {"path": path, "type": "org", "name": "株式会社テスト", "role": "競合"})
        self.assertIn("テスト", [e["key"] for e in r["entities"]])
        st, r = self.call("POST", "/api/people/profile", {"type": "person", "key": "田中"})
        self.assertEqual(st, 200)
        self.assertFalse(r["llm"])
        st, r = self.call("GET", "/api/graph?people=1")
        self.assertTrue(any(n["kind"] == "person" for n in r["nodes"]))
        st, r = self.call("POST", "/api/people/extract", {})
        self.assertEqual(st, 200)                                                  # ジョブは開始し、LLM 未設定で失敗する
        for _ in range(50):
            st, ix = self.call("GET", "/api/index/status")
            if ix["job"]["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(ix["job"]["state"], "error")
        self.assertIn("LLM", ix["job"]["message"])


if __name__ == "__main__":
    unittest.main()
