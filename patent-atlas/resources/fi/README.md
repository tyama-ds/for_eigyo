# FI official data

`fi_202604.json.gz` contains 190,202 FI symbols across sections A–H. English
titles and parent relationships come from JPO's nested FI 2026.04 XML, published
in the May 2026 ZIP. Group headers, notes and indexes that are not classification
entries are not treated as symbols. Identical repeated XML entries are deduplicated;
conflicting definitions or parents fail the build. No hierarchy is inferred from
the digits or from IPC.

Japanese titles for 188,621 FI symbols come independently from JPO's
IPC–FI–CPC parallel viewer data dated 2026-08-18. A missing Japanese title falls
back to the English definition. These editions are displayed separately.
The same download supplies Japanese IPC captions for all 80,145 bundled WIPO
2026.01 symbols; those captions are validated independently against IPC.

The FI-to-IPC concordance is read from the official CSVs inside the FI ZIP.
An application candidate is emitted only if the mapped IPC exists in the bundled
IPC edition. This is a documented cross-system candidate, not evidence that the
IPC was actually assigned to a particular patent. Original FI suffixes are kept.

Source URLs and SHA-256 hashes are recorded in `metadata.json` and the bundles.
Publication page: https://www.jpo.go.jp/e/system/patent/gaiyo/seido-bunrui/bulkdata.html

Rebuild from the downloaded official ZIPs (no automatic network access):

```powershell
python resources/fi/build_full_catalog.py PATH/fi_xml202605.zip PATH/data_20260818.zip
```

Records use `[code, English title, parent, level, dots, Japanese title, mapped IPCs]`.
This is a classification-title and hierarchy catalogue, not a reproduction of
all explanatory notes, figures, or legal scope. Review the official tables and
actual patent documents for suitability. The previous EV-only 255-code bundle
and its topic-specific builder have been removed.
