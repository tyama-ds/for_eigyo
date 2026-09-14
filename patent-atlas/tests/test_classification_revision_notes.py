"""Historical IPC notes must not guess validity or mutate future lookups."""
import unittest

import classification_catalog
import classification_revision_notes as revision_notes


class ClassificationRevisionNotesTests(unittest.TestCase):
    def test_retired_code_explains_version_change_without_replacement(self):
        note = revision_notes.lookup("G05D1/02")
        self.assertEqual(note["status"], "retired")
        self.assertEqual(note["last_verified_version"], "2023.01")
        self.assertEqual(note["changed_version"], "2024.01")
        self.assertIn("1対1の置換ではない", note["explanation"])
        self.assertNotIn("replacement", note)
        self.assertNotIn("replacement_code", note)
        self.assertIsNone(classification_catalog.lookup("IPC", note["code"]))
        self.assertTrue(any("20230101" in source["url"] for source in note["sources"]))
        self.assertTrue(any("notion=rcl" in source["url"] for source in note["sources"]))

    def test_formatting_variations_do_not_change_historical_identity(self):
        for value in ("g05d 1/02", "Ｇ０５Ｄ １／０２", "G05D0001020000"):
            with self.subTest(value=value):
                self.assertEqual(revision_notes.lookup(value)["code"], "G05D1/02")

    def test_unlisted_and_incomplete_codes_are_not_inferred(self):
        for value in (None, "", "G05D", "G05D1", "G05D1/2", "G05D1/020", "G05D1/00",
                      "G05D1/43", "B60L33/00", "G05D1/02, G05D1/00", "IPC:G05D1/02"):
            with self.subTest(value=value):
                self.assertIsNone(revision_notes.lookup(value))

    def test_returned_note_cannot_modify_later_lookup(self):
        changed = revision_notes.lookup("G05D1/02")
        changed["status"] = "valid"
        changed["sources"][0]["url"] = "https://example.invalid/"
        changed["sources"].clear()
        fresh = revision_notes.lookup("G05D1/02")
        self.assertEqual(fresh["status"], "retired")
        self.assertEqual(len(fresh["sources"]), 3)
        self.assertTrue(all("wipo.int/" in source["url"] for source in fresh["sources"]))


if __name__ == "__main__":
    unittest.main()
