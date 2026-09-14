"""最小限の JSON スキーマ検証（標準ライブラリのみ）。

対応: type（単一／リスト）, required, properties, additionalProperties, items, enum,
minimum, maximum, minLength, minItems, maxItems, patternProperties（値スキーマのみ）。
"""
from __future__ import annotations

_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict, "null": type(None),
}


def _type_ok(value, t: str) -> bool:
    py = _TYPES.get(t)
    if py is None:
        return True
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, py)


def validate(value, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors
    types = schema.get("type")
    if types:
        allowed = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, t) for t in allowed):
            errors.append(f"{path}: 型が {allowed} ではありません（{type(value).__name__}）")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 値 {value!r} は enum {schema['enum']} に含まれません")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: 文字数が minLength {schema['minLength']} 未満です")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 要素数が minItems {schema['minItems']} 未満です")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 要素数が maxItems {schema['maxItems']} を超えています")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                errors.extend(validate(item, item_schema, f"{path}[{i}]"))
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}: 必須キー {key!r} がありません")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
        pattern_props = schema.get("patternProperties") or {}
        extra = [k for k in value if k not in props]
        if pattern_props:
            for k in extra:
                for _, sub in pattern_props.items():
                    errors.extend(validate(value[k], sub, f"{path}.{k}"))
        elif schema.get("additionalProperties") is False and extra:
            errors.append(f"{path}: 許可されていないキー {extra}")
    return errors


def is_valid(value, schema: dict) -> bool:
    return not validate(value, schema)
