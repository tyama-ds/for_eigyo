"""graphio（LightRAG / nano-graphrag の GraphML 読み込み）のユニットテスト。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ragcore.graphio import graphml_graph_payload, load_graphml  # noqa: E402

# LightRAG（NetworkX ストレージ）が出力する形式を模した GraphML。
# 旧版は名前を "引用符" で包むため、その剥がしも検証する。
FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="d0" for="node" attr.name="entity_type" attr.type="string"/>
  <key id="d1" for="node" attr.name="description" attr.type="string"/>
  <key id="d2" for="node" attr.name="entity_id" attr.type="string"/>
  <key id="d3" for="edge" attr.name="weight" attr.type="double"/>
  <key id="d4" for="edge" attr.name="description" attr.type="string"/>
  <graph edgedefault="undirected">
    <node id="&quot;青嶺製作所&quot;">
      <data key="d0">"organization"</data>
      <data key="d1">"産業機器メーカー"</data>
    </node>
    <node id="&quot;SKYEDGE&quot;">
      <data key="d0">"product"</data>
      <data key="d1">"ワイヤレスセンサーのブランド"</data>
      <data key="d2">"SkyEdge"</data>
    </node>
    <node id="&quot;北浜電機&quot;">
      <data key="d0">"organization"</data>
      <data key="d1">"制御盤メーカー"</data>
    </node>
    <node id="&quot;孤立ノード&quot;">
      <data key="d0">"concept"</data>
    </node>
    <edge source="&quot;青嶺製作所&quot;" target="&quot;SKYEDGE&quot;">
      <data key="d3">9.0</data>
      <data key="d4">"ブランドを展開"</data>
    </edge>
    <edge source="&quot;SKYEDGE&quot;" target="&quot;北浜電機&quot;">
      <data key="d3">7.0</data>
      <data key="d4">"代理店網で販売"</data>
    </edge>
  </graph>
</graphml>
"""


class TestLoadGraphml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "graph_chunk_entity_relation.graphml"
        cls.path.write_text(FIXTURE, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_nodes_parsed_with_quote_stripping(self):
        nodes, edges = load_graphml(self.path)
        self.assertEqual(len(nodes), 4)
        by_name = {n["name"]: n for n in nodes}
        self.assertIn("青嶺製作所", by_name)
        self.assertEqual(by_name["青嶺製作所"]["type"], "organization")
        self.assertEqual(by_name["青嶺製作所"]["description"], "産業機器メーカー")
        # entity_id データがあればそちらを表示名に使う
        self.assertIn("SkyEdge", by_name)

    def test_edges_parsed(self):
        _, edges = load_graphml(self.path)
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0]["strength"], 9)
        self.assertEqual(edges[0]["description"], "ブランドを展開")

    def test_payload_communities_and_degree(self):
        payload = graphml_graph_payload(self.path)
        self.assertNotIn("error", payload)
        self.assertEqual(len(payload["nodes"]), 4)
        self.assertEqual(len(payload["edges"]), 2)
        by_name = {n["name"]: n for n in payload["nodes"]}
        self.assertEqual(by_name["SkyEdge"]["degree"], 2)
        self.assertEqual(by_name["孤立ノード"]["degree"], 0)
        # 連結3ノードが同じコミュニティ、孤立ノードは別
        self.assertEqual(by_name["青嶺製作所"]["community"],
                         by_name["北浜電機"]["community"])
        self.assertNotEqual(by_name["青嶺製作所"]["community"],
                            by_name["孤立ノード"]["community"])
        # サイズ2以上のグループにはメンバー一覧の summary が付く
        top = payload["communities"][0]
        self.assertEqual(top["size"], 3)
        self.assertIn("メンバー", top["summary"])

    def test_max_nodes_truncation(self):
        payload = graphml_graph_payload(self.path, max_nodes=2)
        self.assertEqual(len(payload["nodes"]), 2)
        self.assertTrue(payload["truncated"])
        # 次数上位（SkyEdge=2）が残る
        self.assertIn("SkyEdge", {n["name"] for n in payload["nodes"]})

    def test_missing_file(self):
        payload = graphml_graph_payload(Path(self.tmp.name) / "nai.graphml")
        self.assertIn("error", payload)
        self.assertIn("インデックス構築", payload["error"])

    def test_broken_xml(self):
        broken = Path(self.tmp.name) / "broken.graphml"
        broken.write_text("<graphml><node", encoding="utf-8")
        payload = graphml_graph_payload(broken)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
