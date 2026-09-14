import unittest

from helpers import sample_query
from pqb.core.dsl import Query, Term
from pqb.core.match import match_ids
from pqb.core.render import ParseError, load_dialect, parse, render, render_parts, roundtrip_ok, split_query
from pqb.core.document import Document


class TestDSL(unittest.TestCase):
    def test_validate_and_json_roundtrip(self):
        q = sample_query()
        self.assertEqual(q.validate(), [])
        q2 = Query.from_json(q.to_json())
        self.assertEqual(q2.signature(), q.signature())
        self.assertEqual(q2.blocks[0].terms[2].status, "rejected")     # rejected も履歴として残る
        self.assertEqual(q.complexity(), 3 + 1 + 1 + 1)                 # 語2+コード1+Block1, 語1+Block1

    def test_validate_errors(self):
        q = sample_query()
        q.blocks[0].terms[0].status = "weird"
        q.variant = "x"
        errors = q.validate()
        self.assertTrue(any("variant" in e for e in errors))
        self.assertTrue(any("status" in e for e in errors))

    def test_fields_ordering(self):
        t = Term("x", fields=["CL", "AB", "AB"])
        self.assertEqual(t.fields, ["AB", "CL"])


class TestRender(unittest.TestCase):
    def test_render_both_dialects_roundtrip(self):
        q = sample_query()
        for did in ("jplatpat", "generic"):
            d = load_dialect(did)
            text = render(q, d)
            self.assertIn("高強度鋼板", text)
            self.assertNotIn("旧語", text)                             # rejected は出ない
            self.assertTrue(roundtrip_ok(q, d), did)
            p = parse(text, d)
            self.assertEqual(p.blocks[0].axis_id, "A")
            self.assertEqual(sorted(t.text for t in p.blocks[0].adopted_terms()), ["高張力鋼板", "高強度鋼板"])

    def test_single_block_with_and_join_roundtrip(self):
        q = sample_query(join="AND")
        q.blocks = q.blocks[:1]                                        # 1 Block だけ（外側の括弧を剥がすと 2 Block に誤読される形）
        for did in ("jplatpat", "generic"):
            d = load_dialect(did)
            self.assertTrue(roundtrip_ok(q, d), (did, render(q, d)))
            self.assertEqual(len(parse(render(q, d), d).blocks), 1)

    def test_and_join_and_exclusion_roundtrip(self):
        q = sample_query(join="AND")
        q.variant = "narrow"
        from pqb.core.dsl import Block
        q.exclusions = [Block(axis_id="X1", terms=[Term("アルミ")])]
        for did in ("jplatpat", "generic"):
            d = load_dialect(did)
            text = render(q, d)
            self.assertTrue(roundtrip_ok(q, d), (did, text))
            p = parse(text, d)
            self.assertEqual(p.blocks[0].effective_join(), "AND")
            self.assertEqual(len(p.active_exclusions()), 1)

    def test_hyphen_in_term_is_not_not(self):
        q = sample_query()
        q.blocks[1].terms = [Term("F-term")]
        d = load_dialect("jplatpat")
        self.assertTrue(roundtrip_ok(q, d))

    def test_parse_human_written_expression(self):
        d = load_dialect("jplatpat")
        q = parse("[高強度鋼板/AB+高張力鋼板/AB+C22C38/00/FI]*[焼入れ/AB+焼入れ/CL]", d)
        self.assertEqual(len(q.blocks), 2)
        self.assertEqual(q.blocks[0].adopted_codes()[0].code, "C22C38/00")
        self.assertEqual(q.blocks[1].adopted_terms()[0].fields, ["AB", "CL"])
        q2 = parse("[high strength steel sheet/AB+高強度鋼板/AB]*[quenching/AB]", d)   # 引用符の無い方言でも空白入りの語を扱う
        self.assertEqual(q2.blocks[0].adopted_terms()[0].text, "high strength steel sheet")
        with self.assertRaises(ParseError):
            parse("[高強度鋼板/ZZ]", d)
        with self.assertRaises(ParseError):
            parse("[高強度鋼板/AB", d)

    def test_split_by_char_limit_keeps_union(self):
        q = sample_query()
        q.blocks[0].terms = [Term(f"語{i:03d}ながいながいながい") for i in range(120)] + [Term("高強度鋼板")]
        d = load_dialect("jplatpat")
        parts = render_parts(q, d)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(not p["too_long"] for p in parts))
        docs = [Document("D1", title="t", abstract="高強度鋼板 焼入れ", codes={"FI": []}),
                Document("D2", title="t", abstract="語050ながいながいながい 焼入れ"),
                Document("D3", title="t", abstract="語119ながいながいながい 焼入れ"),
                Document("D4", title="鋼板", abstract="焼入れ", codes={"FI": ["C22C38/04"]}),
                Document("D5", title="無関係", abstract="焼入れ")]
        base = match_ids(q, docs)
        union = set()
        for part in split_query(q, d):
            union |= match_ids(part, docs)
        self.assertEqual(union, base)
        self.assertEqual(base, {"D1", "D2", "D3", "D4"})


if __name__ == "__main__":
    unittest.main()
