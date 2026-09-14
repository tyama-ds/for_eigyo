# WIPO IPC 2026.01 catalogue

`ipc_2026_01_en.json.gz` is a compact, offline derivative of the official English
IPC 2026.01 Scheme Master File, cross-checked against the official valid-symbol
list. Source download URLs, timestamps, hashes and counts are in `metadata.json`.
WIPO is the source/publisher of the classification text.

The `parent` relationship comes from actual XML nesting. Guidance headings,
notes and subclass indexes are not classification symbols and are omitted from
the selectable catalogue. Indexing codes in the valid-symbol list are included
and marked `entry_type: I`.

Titles preserve the English title text and references, with plain-text chemical
sub/superscripts and `[illustration]` placeholders. For notes, illustrations and
complete interpretation, consult the WIPO link attached to every record.
These are official English titles; no Japanese translation or AI interpretation
is represented as official text. CPC, FI and F-term are not part of this data.

To rebuild from the two public WIPO downloads:

```powershell
.\.venv\Scripts\python.exe resources/ipc/build_catalog.py
```

The app does not download data or call WIPO when a user browses classifications.

## Japanese display captions

`ipc_ja_pmgs.json.gz` adds 1,657 Japanese captions from 64 official J-PlatPat PMGS
tables, retrieved on 2026-09-12. It covers the current battery/material/process
exploration, its directly browsable tables, and all eight sections/classes.
`metadata_ja.json` records each source URL and SHA-256 hash. The requested seed
symbols are in `japanese_snapshot_codes.json`.

This is a **bounded Japanese publication snapshot**, not the full Japanese IPC
catalogue. Symbols are checked against the bundled WIPO 2026.01 valid symbols;
PMGS does not declare an overall wording edition on these pages, so the Japanese
wording is not represented as independently verified 2026.01 text. Japanese
subgroup captions need their parent context. Notes and illustrations remain in
the linked publication.

The display fields `title_ja` / `title_en` do not replace the canonical title,
hierarchy, semantic-layout inputs or search symbols. An explicit UI action can
retrieve missing Japanese captions from up to four PMGS tables, using the app's
proxy and CA settings. That cache contains captions and source metadata only.
Ordinary browsing and language switching use the local bundle/cache.

To rebuild this same bounded snapshot:

```powershell
.\.venv\Scripts\python.exe resources/ipc/build_japanese_catalog.py --codes resources/ipc/japanese_snapshot_codes.json
```

Underlying IPC copyright belongs to WIPO; the Japanese translation belongs to
the Government of Japan, as described on the [JPO publication page](https://www.jpo.go.jp/system/patent/gaiyo/bunrui/ipc/ipc8wk.html).
