import unittest

from helpers import sample_query
from pqb.core.document import Document
from pqb.core.dsl import Block, Code, Term
from pqb.core.match import block_matches, match_ids, query_matches
from pqb.knowledge import codes as C
from pqb.knowledge.codes import CodeDictionary


class TestCodes(unittest.TestCase):
    def test_levels(self):
        self.assertEqual([v for _, v in C.levels("FI", "C22C38/04,301@A")],
                         ["C22C", "C22C38/00", "C22C38/04", "C22C38/04,301", "C22C38/04,301@A"])
        self.assertEqual([lv for lv, _ in C.levels("FT", "4K037AA01")], ["theme", "theme_view", "full"])
        self.assertEqual([v for _, v in C.levels("IPC", "C22C 38/00")], ["C22C", "C22C38/00"])
        self.assertEqual(C.levels("FI", "C22C38/00"), [("subclass", "C22C"), ("main_group", "C22C38/00")])
        self.assertEqual(C.level_of("FI", "???"), "raw")

    def test_normalize_and_matches(self):
        self.assertEqual(C.normalize("IPC", " c22c 38／00 "), "C22C38/00")
        self.assertTrue(C.matches("FI", "C22C38/00", "C22C38/04"))
        self.assertTrue(C.matches("FI", "C22C", "C22C38/04,301@A"))
        self.assertFalse(C.matches("FI", "C22C38/04", "C22C38/00"))
        self.assertTrue(C.matches("FT", "4K037", "4K037AA01"))
        self.assertTrue(C.matches("FT", "4K037AA", "4K037AA01"))
        self.assertFalse(C.matches("FT", "4K037AA01", "4K037AA02"))
        self.assertEqual(C.coarser("FI", "C22C38/04"), "C22C38/00")
        self.assertIsNone(C.coarser("FI", "C22C"))

    def test_dictionary_degraded_and_official(self):
        d = CodeDictionary()
        self.assertTrue(d.degraded)
        d.observe("FI", "C22C38/04")
        self.assertTrue(d.known("FI", "C22C38/00"))     # 縮退モード: 観測コード（全粒度）を既知扱い
        d.load_csv_text("scheme,code,title,parent,level\nFI,C21D1/18,焼入れ,C21D1/00,subgroup\n")
        self.assertFalse(d.degraded)
        self.assertTrue(d.known("FI", "C21D1/18"))
        self.assertFalse(d.known("FI", "C22C38/04"))    # 公式データがあれば観測のみは未照合
        self.assertEqual(d.title("FI", "C21D1/18"), "焼入れ")
        self.assertEqual(d.parents("FI", "C22C38/04,301"), ["C22C", "C22C38/00", "C22C38/04"])


class TestMatch(unittest.TestCase):
    def setUp(self):
        self.docs = [Document("D1", title="高強度鋼板の製造方法", abstract="焼入れ工程を含む", codes={"FI": ["C22C38/04,301@A"]}),
                     Document("D2", title="鋼板", abstract="焼入れ", codes={"FI": ["C21D9/46"]}),
                     Document("D3", title="アルミ材", abstract="焼入れ", codes={"FI": ["C22C38/00"]}),
                     Document("D4", title="高張力鋼板", claims="焼き入れ", codes={})]

    def test_or_join(self):
        q = sample_query()
        self.assertEqual(match_ids(q, self.docs), {"D1", "D3"})

    def test_and_join_and_fields(self):
        q = sample_query(join="AND")
        self.assertEqual(match_ids(q, self.docs), {"D1"})
        q2 = sample_query()
        q2.blocks[1].terms = [Term("焼き入れ", fields=["AB"])]
        self.assertEqual(match_ids(q2, self.docs), set())
        q2.blocks[1].terms = [Term("焼き入れ", fields=["CL"])]
        self.assertEqual(match_ids(q2, self.docs), {"D4"})

    def test_exclusion_and_normalization(self):
        q = sample_query()
        q.exclusions = [Block(axis_id="X1", terms=[Term("アルミ", fields=["TX"])])]
        self.assertEqual(match_ids(q, self.docs), {"D1"})
        d = Document("D9", title="高強度 鋼板", abstract="ＹＡＫＩＩＲＥ 焼入れ")
        self.assertTrue(query_matches(sample_query(), d))     # 空白・全角の差を吸収

    def test_block_codes_only(self):
        b = Block(axis_id="A", codes=[Code("FI", "C22C38/00")])
        self.assertTrue(block_matches(b, self.docs[0]))
        self.assertFalse(block_matches(b, self.docs[1]))


if __name__ == "__main__":
    unittest.main()
