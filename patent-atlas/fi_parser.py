"""Strict FI field parsing, shared by extraction and classification browsing."""

import re
import unicodedata
import classification_catalog as ipc_catalog
import fi_catalog


def _raw(value):
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        value = "; ".join(value)
    if not isinstance(value, str):
        raise ValueError("分類欄は文字列または文字列配列で指定してください。")
    if len(value) > 12000:
        raise ValueError("分類欄は12,000文字以内で指定してください。")
    return value


def parse_fi(value, *, allow_compact_suffix=False):
    """Consume FI entries completely, including comma expansion / @ identifiers.

    Commas preceding a complete IPC-shaped FI base separate entries; commas
    followed by three digits belong to the FI expansion. Invalid trailing text
    invalidates the field, rather than silently producing a partly parsed hit.
    Manual input also retains its previously supported compact letter suffix.
    """
    text = unicodedata.normalize("NFKC", _raw(value)).strip().upper()
    if not text:
        return []
    base = r"[A-H]\s*\d{2}\s*[A-Z]\s*\d{1,4}\s*[/:]\s*\d{1,6}"
    letter = r"\s*@\s*[A-Z]"
    if allow_compact_suffix:
        letter = r"(?:" + letter + r"|\s*[A-Z](?=$|[\s,;、；|]))"
    entry = re.compile(
        r"("
        + base
        + r")(?P<suffix>(?:\s+\d{3})(?:\s*[A-Z](?=$|[\s,;、；|]))?|\s+[A-Z](?=$|[\s,;、；|])|(?:\s*,\s*\d{3})?(?:"
        + letter
        + r")?)"
    )
    result, position = [], 0
    while position < len(text):
        match = entry.match(text, position)
        if not match:
            raise ValueError(f"FI の未解析部分があります: {text[position : position + 50]}")
        # Validate while the whole field is still being parsed. Otherwise an
        # incomplete FI can escape extract()'s field-level error handling and
        # prevent valid IPC/CPC/F-term fields from being used as well.
        if ":" not in match[1]:
            fi_catalog.normalize(match[0])
        position = match.end()
        version = re.match(r"\s*\(\d{4}\.\d{2}\)", text[position:])
        if version:
            position += version.end()
        result.append(dict(base=ipc_catalog.normalize_code(match[1]), raw=match[0].strip()))
        if position < len(text):
            delimiter = re.match(r"[\s,;、；|]+", text[position:])
            if not delimiter:
                raise ValueError(f"FI の未解析部分があります: {text[position : position + 50]}")
            position += delimiter.end()
    return result
