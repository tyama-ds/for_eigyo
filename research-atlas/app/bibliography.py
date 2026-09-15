"""Conservative normalization of supplied affiliations and reference identifiers.

No author–institution links or citation links are inferred from list positions,
names of institutions, reference counts, or publication titles.
"""

from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
import json
import re
import unicodedata
from urllib.parse import unquote, urlsplit


METADATA_PIPELINE_VERSION = 3
_MISSING = {"", "na", "n/a", "none", "null", "nan", "-", "不明", "欠損"}


def _text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def normalize_affiliations(value) -> list[str]:
    """Split semicolons only; commas belong to institution names and addresses."""
    values = value if isinstance(value, (list, tuple)) else [value]
    result, seen = [], set()
    for item in values:
        if isinstance(item, dict):
            item = item.get("name") or item.get("affiliation") or ""
        if not isinstance(item, str):
            continue
        if item.lstrip().startswith("["):
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                result.extend(normalize_affiliations(parsed))
                continue
        parser = _PlainText()
        parser.feed(item)
        for part in re.split(r"[;；]", "".join(parser.parts)):
            text = _text(part)
            key = text.casefold()
            if key not in _MISSING and key not in seen:
                seen.add(key)
                result.append(text)
    unique = {}
    for text in result:
        unique.setdefault(text.casefold(), text)
    return list(unique.values())


def normalize_doi(value) -> str:
    text = _text(unquote(unescape(str(value or "")))).strip("<>\"'")
    if re.match(r"^https?://(?:dx\.)?doi\.org/", text, re.I):
        text = urlsplit(text).path.lstrip("/")
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", text, flags=re.I)
    text = text.rstrip(".,;")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while text.endswith(closing) and text.count(closing) > text.count(opening):
            text = text[:-1]
    return text.casefold() if re.fullmatch(r"10\.\d{4,9}/\S+", text) else ""


def normalize_reference_id(value) -> str:
    text = _text(value).strip(" \"'<>.,;")
    text = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "arxiv:", text, flags=re.I)
    text = re.sub(r"^https?://pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?$", r"pmid:\1", text, flags=re.I)
    if re.fullmatch(r"(?:eid\s*:\s*|scopus:)?2-s2\.0-\d+", text, re.I):
        return "2-s2.0-" + text.rsplit("-", 1)[1]
    if match := re.fullmatch(r"arxiv:\s*((?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z0-9.-]+/\d{7}))(?:v\d+)?(?:\.pdf)?", text, re.I):
        return "arxiv:" + match.group(1).casefold()
    if match := re.fullmatch(r"(?:pmid|pubmed):\s*(\d+)", text, re.I):
        return "pmid:" + match.group(1)
    if match := re.fullmatch(r"(?:pmcid:\s*)?(PMC\d+)", text, re.I):
        return "pmcid:" + match.group(1).upper()
    if match := re.fullmatch(r"europepmc:(MED|PMC|PPR):([A-Za-z0-9._-]+)", text, re.I):
        return "europepmc:" + match.group(1).upper() + ":" + match.group(2)
    if match := re.fullmatch(r"(?:openalex:|https?://openalex\.org/)(W\d+)", text, re.I):
        return "openalex:" + match.group(1).upper()
    return ""


def _references_from_text(value):
    text = unescape(unquote(_text(value)))
    records = []
    # A standard identifier is required; citation prose and local reference keys
    # do not establish the identity of a cited work.
    for match in re.finditer(r"10\.\d{4,9}/[^\s;\"<>]+", text, re.I):
        if doi := normalize_doi(match.group()):
            records.append({"doi": doi})
    patterns = [r"(?:eid\s*:\s*|scopus:)?2-s2\.0-\d+", r"(?:PMID|pubmed):\s*\d+",
                r"(?:PMCID:\s*)?PMC\d+", r"europepmc:(?:MED|PMC|PPR):[A-Za-z0-9._-]+",
                r"(?:arxiv:|https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/)\s*(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z0-9.-]+/\d{7})(?:v\d+)?(?:\.pdf)?",
                r"(?:openalex:|https?://openalex\.org/)W\d+"]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            if identifier := normalize_reference_id(match.group()):
                records.append({"id": identifier})
    return records


def normalize_references(value) -> list[dict]:
    values = value if isinstance(value, (list, tuple)) else [value]
    records = []
    for item in values:
        if isinstance(item, str):
            if item.lstrip().startswith(("[", "{")):
                try:
                    parsed = json.loads(item)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, (list, dict)):
                    records.extend(normalize_references(parsed))
                    continue
            records.extend(_references_from_text(item))
        elif isinstance(item, dict):
            record = {}
            if doi := normalize_doi(item.get("doi") or item.get("DOI")):
                record["doi"] = doi
            for field in ("id", "eid", "EID", "url", "URL"):
                raw = item.get(field)
                if identifier := normalize_reference_id(raw):
                    record.setdefault("id", identifier)
                elif doi := normalize_doi(raw):
                    record.setdefault("doi", doi)
            if record:
                records.append(record)
            elif item.get("unstructured"):
                records.extend(_references_from_text(item["unstructured"]))
    output, identifiers = [], {}
    for record in records:
        # Index identifiers instead of comparing each reference with every
        # preceding reference. Long Scopus lists otherwise cost O(refs²) for
        # each of hundreds of thousands of papers.
        candidates = {index for field, identifier in record.items()
                      for index in identifiers.get((field, identifier), ())}
        index = next((index for index in sorted(candidates)
                      if not (output[index].get("doi") and record.get("doi")
                              and output[index]["doi"] != record["doi"])), None)
        match = output[index] if index is not None else None
        if match is None:
            index = len(output)
            output.append(deepcopy(record))
        else:
            for field, identifier in record.items():
                match.setdefault(field, identifier)
        # Only retained identifiers participate. Conflicting DOI values and
        # IDs discarded by setdefault must not create a new identity link.
        for field, identifier in output[index].items():
            identifiers.setdefault((field, identifier), set()).add(index)
    return output


def references_status(paper) -> str:
    if normalize_references(paper.get("references")) or paper.get("references_status") == "provided":
        return "provided"
    return "not_provided"


def _name_keys(value):
    text = _text(value)
    values = [text]
    if text.count(",") == 1:
        family, given = text.split(",", 1)
        values.append(given + " " + family)
    return {"".join(c for c in item.casefold() if c.isalnum()) for item in values}


def attach_author_affiliations(authors, value) -> tuple[list[dict], int]:
    """Accept explicit keyed JSON or name-prefixed Scopus affiliation entries."""
    result = deepcopy(authors)
    if not value:
        return result, 0
    raw = value
    if isinstance(raw, str) and raw.lstrip().startswith(("[", "{")):
        try:
            raw = json.loads(raw)
        except ValueError:
            return result, 1
    if isinstance(raw, dict):
        if "affiliations" in raw or "affiliation" in raw:
            entries = [raw]
        else:
            known_ids = {str(identifier) for author in result
                         for identifier in [author.get("id"), *author.get("aliases", [])] if identifier}
            entries = [{("id" if key in known_ids or re.match(r"^(?:scopus|orcid|openalex|name):", key, re.I) else "name"): key,
                        "affiliations": affiliations} for key, affiliations in raw.items()]
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = [part.strip() for part in re.split(r"[;；]", str(raw)) if part.strip()]
    unmatched = 0
    for entry in entries:
        matches, affiliations = [], []
        if isinstance(entry, dict):
            identifier, name = str(entry.get("id") or ""), str(entry.get("name") or "")
            affiliations = normalize_affiliations(entry.get("affiliations") or entry.get("affiliation"))
            exact = [author for author in result if identifier and identifier in [author.get("id"), *author.get("aliases", [])]]
            matches = exact if identifier else [author for author in result if name and _name_keys(author.get("name")) & _name_keys(name)]
        elif isinstance(entry, str):
            parts = entry.split(",")
            for count in range(1, min(3, len(parts) - 1) + 1):
                prefix = ",".join(parts[:count])
                candidates = [author for author in result if _name_keys(author.get("name")) & _name_keys(prefix)]
                if candidates:
                    matches = candidates
                    affiliations = normalize_affiliations(",".join(parts[count:]))
                    break
        if len(matches) != 1 or not affiliations:
            unmatched += 1
            continue
        matches[0]["affiliations"] = normalize_affiliations([*matches[0].get("affiliations", []), *affiliations])
    return result, unmatched


def bibliography_summary(papers) -> dict:
    return {"metadata_pipeline_version": METADATA_PIPELINE_VERSION,
            "affiliation_papers": sum(bool(paper.get("affiliations")) for paper in papers),
            "reference_metadata_papers": sum(references_status(paper) == "provided" for paper in papers),
            "reference_identifier_papers": sum(bool(paper.get("references")) for paper in papers)}
