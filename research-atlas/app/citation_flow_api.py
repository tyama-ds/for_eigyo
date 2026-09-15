"""Read-only citation-centroid maps and exports of the displayed analytical view."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response

from . import citation_flow
from .field_exports import _csv

router = APIRouter()


def flow_csv(value: dict) -> str:
    """Lossless typed leaves, including full-cohort counts and drawing limits."""
    rows = []

    def walk(item, path=""):
        if isinstance(item, dict) and item:
            for key, child in item.items():
                walk(child, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(item, list) and item:
            for index, child in enumerate(item):
                walk(child, path + "/" + str(index))
        else:
            kind = ("null" if item is None else "boolean" if isinstance(item, bool)
                    else "number" if isinstance(item, (int, float))
                    else "object" if isinstance(item, dict) else "array" if isinstance(item, list) else "string")
            encoded = json.dumps(item, ensure_ascii=False, allow_nan=False) if kind in {"object", "array", "boolean"} else item
            rows.append([value.get("result_id", ""), value.get("flow_id", ""), path, kind, encoded])

    walk(value)
    return _csv(["Analysis ID", "Citation flow ID", "JSON Pointer", "Value type", "Value"], rows)


@router.get("/api/results/{result_id}/citation-flow")
def get_citation_flow(result_id: str, interval: Literal["year", "quarter", "month"] = "year",
                      topic_id: str = Query(default="all", min_length=1, max_length=120)):
    return citation_flow.build_citation_flow(result_id, interval=interval, topic_id=None if topic_id == "all" else topic_id)


@router.get("/api/results/{result_id}/citation-flow/export")
def export_citation_flow(result_id: str, format: Literal["csv", "json"] = "csv",
                         interval: Literal["year", "quarter", "month"] = "year",
                         topic_id: str = Query(default="all", min_length=1, max_length=120)):
    value = get_citation_flow(result_id, interval, topic_id)
    content = flow_csv(value) if format == "csv" else json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    return Response(content, media_type="text/csv" if format == "csv" else "application/json",
                    headers={"Content-Disposition": f'attachment; filename="citation-flow-{result_id}.{format}"'})
