"""Shared primitives and caller policies must preserve distinct domain contracts."""

import copy
import hashlib
import json
import unittest

import catalog
import classification_retrieval
import convergence_engine
import discovery_engine
import discovery_routes
import invention_strategy
import llm_judgment
import orchestration_engine
import query_formats
import research_strategy
import workspace_import
from common import (
    fingerprint,
    is_assessed,
    is_plain_term,
    json_schema,
    keep_rows,
    label_counts,
    normalize_publication_id,
    text_contains,
    tokenize_keywords,
    validate_llm_object,
)


class PublicationKeyTests(unittest.TestCase):
    def test_all_three_callers_share_nfkc_key_without_rewriting_ids(self):
        for identifier in ("JP-2026-123456 A1", " ｊｐ－２０２６－１２３４５６　ａ１ ", "JP2026123456A1"):
            row = {"id": identifier}
            before = copy.deepcopy(row)
            self.assertEqual(normalize_publication_id(identifier), "JP2026123456A1")
            self.assertEqual(workspace_import.publication_key(row), "JP2026123456A1")
            self.assertEqual(discovery_routes._document_key(row), "JP2026123456A1")
            self.assertEqual(convergence_engine.publication_key(identifier), "JP2026123456A1")
            self.assertEqual(row, before)

    def test_non_ascii_and_missing_identifiers_do_not_collapse(self):
        self.assertEqual(normalize_publication_id(" 仮番号 "), "仮番号")
        self.assertEqual(normalize_publication_id("－－"), "--")
        self.assertEqual(normalize_publication_id(None), "")
        rows, duplicates, missing = convergence_engine._rows([{}, {}, {"id": "仮番号"}])
        self.assertEqual((len(rows), duplicates, missing), (3, 0, 2))

    def test_merge_retains_reviewed_row_when_incoming_id_uses_fullwidth(self):
        original = {"id": "JP123A1", "title": "材料", "abstract": "構造", "label": "keep", "label_source": "human"}
        incoming = {**original, "id": "ＪＰ１２３Ａ１", "label": None, "label_source": None}
        merged, report = workspace_import.merge_patents([original], [incoming])
        self.assertEqual(merged, [original])
        self.assertEqual((report["added"], report["matched"]), (0, 1))
        self.assertEqual(incoming["id"], "ＪＰ１２３Ａ１")


class KeywordTests(unittest.TestCase):
    def test_nfkc_delimiters_quotes_and_deduplication(self):
        text = "Ｂａｔｔｅｒｙ；Battery、＂solid electrolyte＂\n膜"
        self.assertEqual(catalog.keywords(text), ["Battery", "solid electrolyte", "膜"])
        self.assertEqual(tokenize_keywords("   ,；、 "), [])

    def test_phrase_mode_preserves_spaces_and_separates_quoted_phrases(self):
        text = 'electric vehicle, "battery charging" "solid electrolyte"'
        self.assertEqual(
            classification_retrieval.phrases(text), ["electric vehicle", "battery charging", "solid electrolyte"]
        )
        self.assertEqual(catalog.keywords("electric vehicle"), ["electric", "vehicle"])
        self.assertEqual(
            tokenize_keywords("electric vehicle\ncharging", split_spaces=False), ["electric vehicle", "charging"]
        )

    def test_malformed_quotes_are_never_silently_discarded(self):
        for text in ('"', '""', '" "', '"a\nb"', '"battery"cell', 'battery"cell"', '"battery""cell"', 'x "" y'):
            for split_spaces in (True, False):
                with self.subTest(text=text, split_spaces=split_spaces), self.assertRaises(ValueError):
                    tokenize_keywords(text, split_spaces=split_spaces)

    def test_keyword_limits_remain_separate_from_definition_descriptions(self):
        with self.assertRaises(ValueError):
            catalog.keywords("x" * 501)
        with self.assertRaises(ValueError):
            catalog.keywords(",".join("word" + str(i) for i in range(25)))
        self.assertEqual(len(classification_retrieval.phrases(",".join("word" + str(i) for i in range(25)))), 24)
        description = "structure and manufacturing " * 80
        self.assertEqual(classification_retrieval.phrases(description), [description.strip()])
        with self.assertRaises(ValueError):
            tokenize_keywords(None)

    def test_editable_terms_share_strict_nfkc_operator_validation(self):
        for term in ("battery and cell", "battery ＡＮＤ cell", "x NEAR3 y", "x ＋ y", "ｂａｔｔｅｒｙ／ＴＸ", "x\ny"):
            with self.subTest(term=term):
                self.assertFalse(is_plain_term(term))
                for validate in (research_strategy._term, invention_strategy._term):
                    with self.assertRaises(ValueError):
                        validate(term)
        for term in ("battery", "solid-state", "製造工程", "ｓｅｎｓｏｒ"):
            self.assertTrue(is_plain_term(term))
            self.assertEqual(research_strategy._term(term), term)
            self.assertEqual(invention_strategy._term(term), term)

    def test_export_literal_policy_keeps_quoted_words_and_db_validation(self):
        for term in ("NEAR3", "ADJ2", "battery AND cell"):
            self.assertFalse(is_plain_term(term))
            self.assertTrue(is_plain_term(term, literal=True))
            self.assertEqual(query_formats.tree_from_data({"op": "text", "value": term}).value, term)
        for term in ("ＡＮＤ", "AND", "x＋y", "x/TX"):
            self.assertFalse(is_plain_term(term, literal=True))
        with self.assertRaisesRegex(ValueError, "演算子だけの語句"):
            query_formats.tree_from_data({"op": "text", "value": "AND"})
        with self.assertRaises(ValueError):
            query_formats.export_query({"keywords": ["NEAR3"], "classifications": []}, "uspto")


class TextMatchingTests(unittest.TestCase):
    def test_matching_normalizes_both_sides_without_mutation(self):
        row = {"title": "ＳＥＮＳＯＲ", "abstract": "電極とﾘﾁｳﾑ"}
        before = copy.deepcopy(row)
        for term in ("sensor", "Ｓｅｎｓｏｒ", "リチウム", "電極"):
            self.assertTrue(text_contains(row, term))
        self.assertFalse(text_contains(row, "camera"))
        self.assertFalse(text_contains(row, ""))
        self.assertFalse(text_contains({"title": None}, "None"))
        self.assertEqual(row, before)

    def test_verbatim_quotes_keep_spelling_case_and_field_boundaries(self):
        row = {"title": "Sensor", "abstract": "材料"}
        self.assertTrue(text_contains(row, "Sensor", exact=True, separate_fields=True))
        for term in ("sensor", "Ｓｅｎｓｏｒ", "Sensor 材料"):
            self.assertFalse(text_contains(row, term, exact=True, separate_fields=True))
        sources = {"source": ["Sensor", "材料"]}
        with self.assertRaises(ValueError):
            invention_strategy._evidence([{"source_id": "source", "quote": "sensor"}], sources)
        self.assertEqual(research_strategy._evidence_ids(["Sensor 材料"], [{"id": "p", **row}]), [])

    def test_map_text_bound_and_evidence_callers(self):
        row = {"id": "p", "title": "x" * 6000, "abstract": "SENSOR"}
        self.assertFalse(text_contains(row, "sensor", max_length=6000))
        self.assertTrue(text_contains(row, "sensor"))
        row = {"id": "p", "title": "ＳＥＮＳＯＲ", "abstract": ""}
        self.assertEqual(research_strategy._evidence_ids(["sensor"], [row]), ["p"])
        self.assertEqual(
            discovery_engine._facets_for(row, [{"id": "sense", "terms": [], "english_terms": ["sensor"]}]), ["sense"]
        )


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"id": "h", "label": "keep", "label_source": "human"},
            {"id": "a", "label": "exclude", "label_source": "agent"},
            {"id": "hold", "label": None, "label_source": "agent", "label_reason": "保留", "agent_confidence": 0.5},
            {"id": "score", "label": None, "score": 0.99},
            {"id": "human-reset", "label": None, "label_source": "human"},
        ]

    def test_scores_never_become_labels_and_source_filters_preserve_order(self):
        self.assertEqual(keep_rows(self.rows), [self.rows[0]])
        self.assertEqual(keep_rows(self.rows, label=("keep", "exclude")), self.rows[:2])
        self.assertEqual(keep_rows(self.rows, label="exclude", source="human"), [])
        self.assertEqual(label_counts(self.rows), {"keep": 1, "exclude": 1, "deferred": 1, "unprocessed": 2})
        self.assertEqual(
            label_counts(self.rows, source="human"), {"keep": 1, "exclude": 0, "deferred": 0, "unprocessed": 1}
        )

    def test_hold_requires_saved_reason_and_finite_non_boolean_confidence(self):
        for bad in (True, None, "0.5", float("nan"), float("inf"), -0.1, 1.1):
            self.assertFalse(is_assessed({**self.rows[2], "agent_confidence": bad}))
        self.assertFalse(is_assessed({**self.rows[2], "label_reason": " "}))
        self.assertIs(llm_judgment.is_assessed, is_assessed)

    def test_convergence_and_orchestration_keep_different_human_count_contracts(self):
        counts = convergence_engine._counts(self.rows)
        summary = orchestration_engine.summary(self.rows)
        self.assertEqual(counts["human_count"], 1)
        self.assertEqual(summary["human"], 2)
        self.assertEqual((counts["keep_count"], summary["keep"]), (1, 1))
        self.assertEqual((counts["deferred_count"], summary["deferred"]), (1, 1))
        legacy = {**self.rows[2], "label": "unsure"}
        self.assertEqual(label_counts([legacy])["deferred"], 1)
        self.assertEqual(label_counts([legacy], deferred_empty_only=True)["deferred"], 0)


class FingerprintAndSchemaTests(unittest.TestCase):
    def test_fingerprint_retains_json_bytes_and_rejects_non_finite_values(self):
        obj = {"語句": ["材料", "膜"], "v": 1}
        expected = hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        self.assertEqual(fingerprint(obj), expected)
        self.assertEqual(fingerprint(dict(reversed(list(obj.items())))), expected)
        self.assertNotEqual(fingerprint({"v": False}), fingerprint({"v": 0}))
        with self.assertRaises(ValueError):
            fingerprint({"v": float("nan")})
        self.assertIsNone(llm_judgment.context_fingerprint(None))
        self.assertEqual(llm_judgment.context_fingerprint(obj), expected)

    def test_dataset_fingerprint_still_ignores_labels_scores_and_row_order(self):
        rows = [{"id": "b", "title": "装置"}, {"id": "a", "title": "材料"}]
        expected = orchestration_engine.fingerprint(rows)
        changed = [{**row, "label": "keep", "score": 1, "x": 10} for row in reversed(rows)]
        self.assertEqual(orchestration_engine.fingerprint(changed), expected)
        changed[0]["title"] = "別の材料"
        self.assertNotEqual(orchestration_engine.fingerprint(changed), expected)

    def test_object_validation_keeps_required_optional_and_extra_policies(self):
        obj = {"required": [], "optional": "value"}
        self.assertIs(validate_llm_object(obj, ["required"], optional_keys=["optional"]), obj)
        for invalid in (None, [], {}, {"required": [], "unexpected": True}):
            with self.assertRaises(ValueError):
                validate_llm_object(invalid, ["required"])
        self.assertIs(validate_llm_object(obj, ["required"], allow_extra=True), obj)
        with self.assertRaises(ValueError):
            validate_llm_object({"extra": 1}, [], any_keys=["english_terms", "facets"], allow_extra=True)
        self.assertEqual(validate_llm_object({"facets": []}, [], any_keys=["facets"], allow_extra=True), {"facets": []})

    def test_schema_children_are_detached_and_optional_fields_stay_optional(self):
        properties = {"required": {"type": "array", "items": {"type": "string"}}, "optional": {"type": "string"}}
        schema = json_schema(properties, required=["required"])
        self.assertEqual(schema["required"], ["required"])
        self.assertFalse(schema["additionalProperties"])
        schema["properties"]["required"]["items"]["type"] = "integer"
        self.assertEqual(properties["required"]["items"]["type"], "string")
        self.assertEqual(json_schema(properties)["required"], ["required", "optional"])
        research = research_strategy.response_schema(["p"])["properties"]["concepts"]["items"]
        self.assertNotIn("or_group", research["required"])
        self.assertIn("or_group", research["properties"])
        self.assertEqual(
            llm_judgment.response_schema(["p"])["properties"]["decisions"]["items"]["properties"]["id"]["enum"], ["p"]
        )


if __name__ == "__main__":
    unittest.main()
