"""Pure shared primitives; caller policies and domain validation remain explicit."""

import copy
import hashlib
import json
import math
import re
import unicodedata


def normalize_publication_id(value):
    """Return an NFKC comparison key, never replace the document's original ID."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return re.sub(r"[^A-Z0-9]", "", text) or text


def is_plain_term(value, *, literal=False):
    """Check syntax on NFKC text without rewriting the user's spelling.

    Export literals are quoted by their renderer, so operator-looking words
    inside a phrase (or NEAR3 alone) remain literal there. Editable term inputs
    use the stricter default; neither policy accepts executable punctuation.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    text = unicodedata.normalize("NFKC", value)
    if re.search(r"""[\x00-\x1f\x7f"'\[\](){}\\/:=+*?$#^~<>|!;]""", text):
        return False
    operators = r"AND|OR|NOT|ANDNOT|NEAR|ADJ|SAME|WITH"
    pattern = rf"^(?:{operators})$" if literal else r"\b(?:AND|OR|NOT|ANDNOT|NEAR\d*|ADJ\d*|PROX\d*|SAME|WITH)\b"
    return re.search(pattern, text, re.I) is None


def tokenize_keywords(text, *, split_spaces=True, limit=24, truncate=False, validate_terms=True, max_term_length=500):
    """Split quoted phrases with one bounded scanner.

    Dictionary/English seed inputs preserve unquoted spaces within a phrase;
    the general keyword field splits them. Features extracted from document
    text (TF-IDF tokens) are not keyword-input tokens.

    Definition retrieval uses validate_terms=False and max_term_length=None:
    natural-language descriptions there are not executable query literals.
    """
    if not isinstance(text, str):
        raise ValueError("キーワードは文字列で入力してください。")
    # Normalize syntax (including full-width quotes and delimiters), not the
    # stored source document. Every caller uses the same quote validation.
    text = unicodedata.normalize("NFKC", text).strip()
    delimiters = r"[\s,、;]"
    bare = r'[^\s,、;" ]+' if split_spaces else r'[^,、;\r\n"]+'
    if text.count('"') % 2:
        raise ValueError("フレーズを囲む二重引用符が閉じていません。")
    out = []
    end = 0
    for match in re.finditer(r'"([^"\r\n]+)"|(' + bare + r")", text):
        if '"' in text[end : match.start()]:
            raise ValueError("各キーワードは1〜500文字にしてください。")
        term = (match[1] if match[1] is not None else match[2]).strip()
        if match[1] is not None and (
            (match.start() and not re.match(delimiters, text[match.start() - 1]))
            or (match.end() < len(text) and not re.match(delimiters, text[match.end()]))
        ):
            raise ValueError("フレーズ同士は空白またはカンマで区切ってください。")
        end = match.end()
        if not term and match[1] is None:
            continue
        if not term or (max_term_length is not None and len(term) > max_term_length):
            raise ValueError("各キーワードは1〜500文字にしてください。")
        if validate_terms and not is_plain_term(term):
            raise ValueError(
                '入力欄はキーワード用です。Boolean式は入力できません。複合語は "solid electrolyte" のように二重引用符で囲んでください。'
            )
        if term and term not in out:
            out.append(term)
    if '"' in text[end:]:
        raise ValueError("各キーワードは1〜500文字にしてください。")
    if limit is not None and len(out) > limit:
        if truncate:
            return out[:limit]
        raise ValueError(f"キーワードは{limit}個までです。超過分を削除せず入力を停止しました。")
    return out


def text_contains(row, term, *, exact=False, separate_fields=False, max_length=None):
    """Test title/abstract text; verbatim quotations use exact separate fields.

    Search checks use NFKC + casefold. Quotes must keep their original spelling,
    case and field boundary, so they explicitly opt out of that normalization.
    """
    fields = [str(row.get(name) or "") for name in ("title", "abstract")]
    texts = fields if separate_fields else [" ".join(fields)]
    if max_length is not None:
        texts = [text[:max_length] for text in texts]
    if not isinstance(term, str) or not term:
        return False
    normalize = (lambda value: value) if exact else (lambda value: unicodedata.normalize("NFKC", value).casefold())
    needle = normalize(term)
    return any(needle in normalize(text) for text in texts)


def is_assessed(row):
    """Recognize a saved verdict, including deferred judgments from older agents."""
    if row.get("label") in ("keep", "exclude"):
        return True
    if row.get("label_source") != "agent" or not str(row.get("label_reason") or "").strip():
        return False
    value = row.get("agent_confidence")
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def keep_rows(rows, *, label="keep", source=None):
    """Select explicit labels only. Model scores never become labels here."""
    labels = (label,) if isinstance(label, str) else label
    return [row for row in rows if row.get("label") in labels and (source is None or row.get("label_source") == source)]


def label_counts(rows, *, source=None, deferred_empty_only=False):
    rows = [row for row in rows if source is None or row.get("label_source") == source]
    return dict(
        keep=len(keep_rows(rows)),
        exclude=len(keep_rows(rows, label="exclude")),
        deferred=sum(
            (not row.get("label") if deferred_empty_only else row.get("label") not in ("keep", "exclude"))
            and is_assessed(row)
            for row in rows
        ),
        unprocessed=sum(not is_assessed(row) for row in rows),
    )


def fingerprint(obj):
    """Hash canonical JSON. The caller owns which fields/order define identity."""
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def validate_llm_object(result, required_keys, *, optional_keys=(), allow_extra=False, any_keys=(), message=None):
    """Validate object shape only; semantic and value checks stay with callers."""
    required = set(required_keys)
    valid = isinstance(result, dict) and required <= result.keys()
    if valid and not allow_extra:
        valid = result.keys() <= required | set(optional_keys)
    if valid and any_keys:
        valid = bool(result.keys() & set(any_keys))
    if not valid:
        raise ValueError(message or "LLM応答の必須項目またはオブジェクト形式が不正です。")
    return result


def json_schema(properties, *, required=None, additional_properties=False):
    """Build a detached object schema; never share mutable schema children."""
    return dict(
        type="object",
        additionalProperties=additional_properties,
        properties=copy.deepcopy(properties),
        required=list(properties) if required is None else list(required),
    )
