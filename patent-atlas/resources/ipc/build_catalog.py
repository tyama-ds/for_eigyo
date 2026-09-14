"""Rebuild the bundled IPC 2026.01 catalogue from WIPO public master files.

Run with Python 3.10+: python resources/ipc/build_catalog.py
Only the two public URLs below are downloaded. No runtime network is required.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

BASE = "https://www.wipo.int/classifications/data/ipc/ITSupport_and_download_area/20260101"
SCHEME_URL = BASE + "/MasterFiles/ipc_scheme_20260101.zip"
VALID_URL = BASE + "/valid_symbol_list/ipc_valid_symbols_20260101.zip"
NS = "{http://www.wipo.int/classifications/ipc/masterfiles}"
OUT = Path(__file__).resolve().parent
LEVELS = {"s": "section", "c": "class", "u": "subclass", "m": "main_group"}


def display_symbol(symbol: str) -> str:
    """Decode WIPO's fixed-width symbol, preserving significant subgroup zeros."""
    if len(symbol) <= 4:
        return symbol
    if not re.fullmatch(r"[A-H][0-9]{2}[A-Z][0-9]{10}", symbol):
        raise ValueError(f"Unexpected WIPO symbol: {symbol}")
    subgroup = symbol[8:].rstrip("0").ljust(2, "0")
    return f"{symbol[:4]}{int(symbol[4:8])}/{subgroup}"


def inline_text(element: ET.Element) -> str:
    tag = element.tag.removeprefix(NS)
    if tag == "sref":
        return display_symbol(element.attrib["ref"])
    if tag == "mref":
        return display_symbol(element.attrib["ref"]) + "–" + display_symbol(element.attrib["endRef"])
    if tag == "img":
        return "[illustration]"
    parts = [element.text or ""]
    for child in element:
        parts.append(inline_text(child))
        parts.append(child.tail or "")
    text = "".join(parts)
    return " (" + text.strip() + ")" if tag == "entryReference" else text


def title_text(entry: ET.Element) -> str:
    title = entry.find(f"{NS}textBody/{NS}title")
    if title is None:
        return ""
    return "; ".join(
        re.sub(r"\s+", " ", inline_text(part)).strip()
        for part in title.findall(f"{NS}titlePart")
    )


def obtain(url: str, local_name: str) -> bytes:
    # A local copy is useful during rebuilding, but is never shipped or loaded by the app.
    local = OUT / local_name
    if local.is_file():
        return local.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "PatentAtlas-IPC-Catalog/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def build() -> None:
    source = obtain(SCHEME_URL, "ipc_scheme_20260101.source.zip")
    validity_source = obtain(VALID_URL, "ipc_valid_symbols_20260101.source.zip")
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        xml = archive.read("EN_ipc_scheme_20260101.xml")
    with zipfile.ZipFile(io.BytesIO(validity_source)) as archive:
        validity_xml = archive.read("ipc_valid_symbols_20260101.xml")
    root = ET.fromstring(xml)
    validity = ET.fromstring(validity_xml)
    assert root.get("edition") == validity.get("edition") == "20260101"
    valid_symbols = {x.attrib["symbol"] for x in validity.findall(f"{NS}IPCSymbol")}
    records: list[list] = []
    seen: set[str] = set()

    def visit(entry: ET.Element, parent_code: str | None = None, parent_dots: int = -1) -> None:
        # Only actual classification entries are selectable. Guidance headings,
        # notes and indexes can reuse symbols but must not become fake parents.
        kind = entry.attrib["kind"]
        selectable = kind in LEVELS or kind.isdigit()
        if selectable:
            raw = entry.attrib["symbol"]
            if raw not in valid_symbols or raw in seen:
                raise ValueError(f"Invalid or duplicate selectable symbol: {raw}")
            seen.add(raw)
            code = display_symbol(raw)
            level = LEVELS.get(kind, "subgroup")
            dots = int(kind) if kind.isdigit() else 0
            if level == "subgroup" and dots != parent_dots + 1:
                raise ValueError(f"Unexpected XML hierarchy at {raw}: {parent_dots} -> {dots}")
            title = title_text(entry)
            if not title:
                raise ValueError(f"Missing official title: {raw}")
            records.append([code, title, parent_code, level, dots, entry.get("entryType", "K"), raw])
            parent_code = code
            parent_dots = dots if level in {"main_group", "subgroup"} else -1
        for child in entry.findall(f"{NS}ipcEntry"):
            visit(child, parent_code, parent_dots)

    for entry in root.findall(f"{NS}ipcEntry"):
        visit(entry)
    assert seen == valid_symbols, f"Missing valid symbols: {sorted(valid_symbols - seen)[:10]}"
    counts = Counter(row[3] for row in records)
    assert counts["section"] == 8
    code_set = {row[0] for row in records}
    assert len(code_set) == len(records)
    assert all(row[2] is None or row[2] in code_set for row in records)
    lookup = {row[0]: row for row in records}
    # Official indentation, not numeric truncation: /0562 is under /0561.
    assert lookup["H01M10/0562"][2] == "H01M10/0561"
    assert lookup["A01B1/04"][2] == "A01B1/02"
    assert lookup["A01B1/06"][2] == "A01B1/00"

    metadata = {
        "kind": "IPC", "version": "2026.01", "edition": "20260101", "language": "en",
        "publisher": "WIPO", "verified": True, "count": len(records),
        "counts_by_level": dict(counts), "valid_symbols_count": len(valid_symbols),
        "source": "https://www.wipo.int/classifications/ipc/en/ITsupport/Version20260101/",
        "scheme_url": SCHEME_URL, "valid_symbols_url": VALID_URL,
        "scheme_zip_sha256": hashlib.sha256(source).hexdigest(),
        "scheme_xml_sha256": hashlib.sha256(xml).hexdigest(),
        "valid_symbols_zip_sha256": hashlib.sha256(validity_source).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hierarchy_method": "Direct nested ipcEntry relationships in WIPO master XML; no symbol truncation",
        "coverage": "All symbols in the WIPO 2026.01 valid-symbol list, including indexing codes",
        "title_format": "Official English titles with references; plain text, illustrations marked [illustration]",
        "limitations": ["English titles only; no automatic Japanese translation", "Classification notes and image contents require consulting the linked WIPO publication", "IPC only; CPC, FI and F-term are separate systems"],
        "record_columns": ["code", "title", "parent", "level", "dots", "entry_type", "symbol"],
    }
    data = json.dumps({"metadata": metadata, "records": records}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "ipc_2026_01_en.json.gz"
    target.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    (OUT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(records), "counts": counts, "compressed_bytes": target.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    build()
