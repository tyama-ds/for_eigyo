"""CSV import, FI enrichment and label export on a Workspace.

The HTTP body is streamed with a hard cap here; the corpus update itself runs
inside the caller's lock/transaction. ``map_patents`` is injectable so the
application keeps its replaceable seam.
"""

import copy
import csv
import io
import json

from analysis_engine import backfill_fi, parse_csv
from convergence_services import new_observation
from workspace_import import merge_patents

MAX_CSV_BYTES = 20 * 1024 * 1024
LABEL_FIELDS = [
    "id",
    "title",
    "abstract",
    "applicant",
    "ipc",
    "fi",
    "fterm",
    "cpc",
    "year",
    "label",
    "label_source",
    "label_reason",
    "score",
]


async def read_csv_request(request):
    """Return (raw bytes, column mapping); an absent Content-Length cannot bypass the cap."""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_CSV_BYTES:
            raise ValueError("CSVは20MB以下にしてください。")
    mapping = json.loads(request.query_params.get("mapping", "{}"))
    if not isinstance(mapping, dict):
        raise ValueError("列の割り当てを確認してください。")
    return bytes(raw), mapping


def checked_merge_mode(request):
    merge_mode = request.query_params.get("merge", "false")
    if merge_mode not in ("true", "false"):
        raise ValueError("追加・置換の指定を確認してください。")
    return merge_mode


def import_patents(ws, parsed, merge_mode, *, map_patents, save=None):
    """Replace or append the corpus from a parsed CSV; the caller holds the lock."""
    state = ws.state
    rows = parsed.pop("rows")
    observation = new_observation(rows, state)
    if merge_mode == "true":
        rows, report = merge_patents(state["patents"], rows)
        parsed.update(import_mode="merge", merge=report)
    else:
        parsed["import_mode"] = "replace"
    clusters = map_patents(rows, state["keywords"])
    training_metadata = copy.deepcopy(state["training"]) if merge_mode == "true" else None
    if training_metadata and report["added"]:
        training_metadata.update(dataset_changed=True, added_since_training=report["added"])
    with ws.transaction(save=save):
        state.update(
            patents=rows,
            clusters=clusters,
            demo=False,
            training=training_metadata,
            import_info=parsed,
            search_result=observation,
        )


def parsed_fi_import(raw, mapping):
    parsed = parse_csv(raw, mapping)
    if parsed["needs_mapping"] or not parsed["mapping"].get("fi"):
        raise ValueError("公報番号・発明の名称・FIを含むCSVを指定してください。")
    return parsed


def enrich_fi(ws, parsed, *, save=None):
    """Fill FI for matching existing rows without reimporting or retraining."""
    state = ws.state
    rows, report = backfill_fi(state["patents"], parsed["rows"])
    if report["conflicts"]:
        raise ValueError(
            "公報番号と内容の不一致、または既存FIとの不一致があるため補完を中止しました。既存判定は変更していません。"
        )
    with ws.transaction(("patents", "import_info"), save=save):
        state["patents"] = rows
        state["import_info"] = {**(state.get("import_info") or {}), "fi_enrichment": report}


def labels_csv(rows):
    """UTF-8 BOM CSV of the judged rows; spreadsheet formula prefixes are neutralized."""
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=LABEL_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(
            {
                k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v
                for k, v in r.items()
            }
        )
    return stream.getvalue().encode("utf-8-sig")
