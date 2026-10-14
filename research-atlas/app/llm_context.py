"""Conservative, model-independent input budgeting without altering measurements.

The estimate is deliberately not described as a tokenizer count. ASCII costs
one token per three characters and non-ASCII costs its UTF-8 byte length, with
additional space for chat templates. Actual server context errors still have a
bounded retry path in the gateway. No tokenizer downloads or model loads occur.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
import re


DEFAULT_CONTEXT_WINDOW = 8192
SAFETY_TOKENS = 512
OMISSION = "\n[…中略：入力上限に合わせて抄録を省略…]\n"
MIN_ABSTRACT_CHARS = 400
BUDGET_ERROR = ("ローカルLLMの入力上限に収まりません。比較期間・論文ID・計測値を保持したまま縮小できる限界に達しました。"
                "ブラウザの接続設定でモデルの実際のコンテキスト長を確認し、回答上限トークンを下げるか、"
                "より大きいコンテキストで読み込んだモデルを選択してください。数値分析は保存されています。")


def serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_chars / 3) + sum(len(char.encode("utf-8")) for char in text if ord(char) >= 128)


def context_integer(value: object) -> int | None:
    if type(value) is int and 512 <= value <= 2_097_152:
        return value
    return None


def active_context(item: dict) -> int | None:
    """Read loaded/configured limits only, never a model's training ceiling."""
    values = [context_integer(item.get(key)) for key in
              ("context_length", "loaded_context_length", "context_window", "n_ctx", "num_ctx")]
    for key in ("config", "load_config"):
        if isinstance(item.get(key), dict):
            values.extend(context_integer(item[key].get(name)) for name in
                          ("context_length", "context_window", "n_ctx", "num_ctx"))
    values = [value for value in values if value is not None]
    return min(values) if values else None


def ollama_configured_context(info: dict) -> int | None:
    parameters = info.get("parameters")
    if isinstance(parameters, dict):
        return context_integer(parameters.get("num_ctx"))
    if isinstance(parameters, str):
        match = re.search(r"(?m)^\s*num_ctx\s+(\d+)\s*$", parameters)
        if match:
            return context_integer(int(match.group(1)))
    return None


def model_context_ceiling(info: dict) -> int | None:
    model_info = info.get("model_info")
    if not isinstance(model_info, dict):
        return None
    values = [context_integer(value) for key, value in model_info.items()
              if key.endswith(".context_length")]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _group(paper: dict) -> tuple:
    # Both temporal sides and both neighboring topics survive any sampling.
    return tuple(str(paper.get(key)) if paper.get(key) is not None else "" for key in ("side", "period", "topic_id"))


def _has_evidence(paper: dict) -> bool:
    return bool(str(paper.get("abstract") or "").strip()
                or any(str(excerpt.get("text") or "").strip() for excerpt in paper.get("excerpts", [])))


def _trim_abstract(paper: dict, size: int):
    text = paper.get("abstract")
    if not isinstance(text, str) or len(text) <= size:
        return
    available = max(2, size - len(OMISSION))
    head, tail = int(available * .6), available - int(available * .6)
    ranges = paper.get("abstract_ranges")
    first, last = (ranges[0][0], ranges[-1][1]) if ranges else (0, len(text))
    paper["abstract"] = text[:head] + OMISSION + text[-tail:]
    paper.setdefault("abstract_original_chars", len(text))
    paper["abstract_sent_chars"] = len(paper["abstract"])
    paper["abstract_ranges"] = [[first, first + head], [last - tail, last]]
    paper["abstract_truncated"] = True
    paper["excerpt_strategy"] = "head_and_tail_context_budget"


def _refresh_summary(payload: dict):
    if not isinstance(payload.get("input_summary"), dict):
        return
    papers = payload.get("papers", [])
    summary = payload["input_summary"]
    def counts(rows):
        return {"paper_count": len(rows), "abstract_count": sum(bool(p.get("abstract", "").strip()) for p in rows)}
    summary.update(counts(papers))
    summary.update(missing_abstract_count=summary["paper_count"] - summary["abstract_count"],
                   truncated_abstract_count=sum(bool(p.get("abstract_truncated")) for p in papers),
                   abstract_chars=sum(len(p.get("abstract", "")) for p in papers))
    for side in ("before", "after", "centroid"):
        summary[side] = counts([paper for paper in papers if paper.get("side") == side])
    summary["excerpt_policy"] = "モデルの入力上限に応じて抄録の冒頭・末尾を保持。期間・領域ごとの根拠を残し、必要時は論文数を削減。送信対象を別途記録。"
    if "excerpt_limit" in payload:
        payload["excerpt_limit"] = summary["excerpt_policy"]


def _prune_references(payload: dict, removed_ids: set[str]):
    """Avoid presenting claims backed only by papers omitted from this request."""
    if not removed_ids:
        return
    def visit(value):
        if isinstance(value, dict):
            for key in ("evidence_ids", "evidence_before", "evidence_after"):
                if isinstance(value.get(key), list):
                    value[key] = [pid for pid in value[key] if pid not in removed_ids]
            if isinstance(value.get("mentions"), list):
                value["mentions"] = [row for row in value["mentions"] if row.get("paper_id") not in removed_ids]
            if isinstance(value.get("citation_links"), list):
                value["citation_links"] = [row for row in value["citation_links"]
                                           if row.get("source_id") not in removed_ids and row.get("target_id") not in removed_ids]
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)


def prepare(payload: dict, instructions: str, schema: dict, *, context_window: int,
            context_source: str, output_tokens: int, retries: int = 0,
            input_token_cap: int | None = None) -> tuple[dict, dict]:
    """Return a deep-copied request and text-free accounting, including on failure.

    An irreducible request is returned with ``fits=False``; the gateway records
    the audit before raising, so unsuccessful calls remain inspectable.
    """
    original = serialize(payload)
    data = deepcopy(payload)
    instruction_tokens = estimate_tokens(instructions)
    schema_tokens = estimate_tokens(serialize(schema))
    available = context_window - output_tokens - SAFETY_TOKENS - instruction_tokens - schema_tokens
    if input_token_cap is not None:
        available = min(available, input_token_cap)
    original_papers = payload.get("papers", [])
    original_paper_ids = [paper.get("id") for paper in original_papers]
    original_excerpts = [item.get("id") for paper in original_papers for item in paper.get("excerpts", [])]
    original_facts = [item.get("id") for item in payload.get("facts", [])]

    def count():
        return estimate_tokens(serialize(data))

    # Gradually shorten all abstracts together, not only the latter period.
    abstract_max = max((len(p.get("abstract", "")) for p in data.get("papers", [])), default=0)
    cap = abstract_max
    while count() > available and cap > MIN_ABSTRACT_CHARS:
        cap = max(MIN_ABSTRACT_CHARS, int(cap * .7))
        for paper in data.get("papers", []):
            _trim_abstract(paper, cap)
        _refresh_summary(data)

    # Excerpt IDs describe indivisible quotes. Never abbreviate their text.
    while count() > available:
        choices = [paper for paper in data.get("papers", []) if len(paper.get("excerpts", [])) > 1]
        if not choices:
            break
        largest = max(choices, key=lambda paper: len(paper["excerpts"]))
        largest["excerpts"].pop()

    # Remove lower-ranked papers round-robin by represented period/topic.
    while count() > available:
        papers = data.get("papers", [])
        groups = Counter(_group(paper) for paper in papers)
        substantiated = Counter(_group(paper) for paper in papers if _has_evidence(paper))
        eligible = [index for index, paper in enumerate(papers) if groups[_group(paper)] > 1
                    and not (_has_evidence(paper) and substantiated[_group(paper)] == 1)]
        if not eligible:
            break
        index = max(eligible, key=lambda i: (groups[_group(papers[i])], not _has_evidence(papers[i]), i))
        removed = papers.pop(index)
        _prune_references(data, {removed.get("id")})
        _refresh_summary(data)

    # Only after keeping fewer, substantive excerpts, allow shorter excerpts
    # when even one paper from every period/topic cannot fit otherwise.
    while count() > available and cap > 120:
        cap = max(120, int(cap * .7))
        for paper in data.get("papers", []):
            _trim_abstract(paper, cap)
        _refresh_summary(data)

    # Completed fact objects retain quotes, provenance and identifiers intact.
    while count() > available and len(data.get("facts", [])) > 1:
        data["facts"].pop()

    sent = serialize(data)
    papers = data.get("papers", [])
    sent_ids = {paper.get("id") for paper in papers}
    sent_excerpts = {item.get("id") for paper in papers for item in paper.get("excerpts", [])}
    sent_facts = {item.get("id") for item in data.get("facts", [])}
    original_by_id = {paper.get("id"): paper for paper in original_papers}
    trimmed = [paper.get("id") for paper in papers
               if paper.get("abstract") != original_by_id[paper.get("id")].get("abstract")]
    reduced = sent != original
    metadata = {"context_window": context_window, "context_source": context_source,
                "input_tokens_estimate": count() + instruction_tokens + schema_tokens,
                "payload_tokens_estimate": count(), "payload_token_budget": max(0, available),
                "instruction_tokens_estimate": instruction_tokens, "schema_tokens_estimate": schema_tokens,
                "output_tokens": output_tokens, "safety_tokens": SAFETY_TOKENS,
                "original_chars": len(original), "sent_chars": len(sent), "reduced": reduced,
                "retries": retries, "request_attempts": retries, "status": "prepared",
                "fits": count() <= available,
                "omitted_paper_ids": [pid for pid in original_paper_ids if pid not in sent_ids],
                "trimmed_paper_ids": trimmed,
                "omitted_excerpt_ids": [eid for eid in original_excerpts if eid not in sent_excerpts],
                "omitted_fact_ids": [fid for fid in original_facts if fid not in sent_facts],
                "warnings": (["入力上限に合わせて根拠の抜粋・選択を縮小しました。解釈の対象は実際に送信した資料に限られます。"] if reduced else [])
                            + ["トークン数は文字種に基づく保守的な推定です。モデルの実測値ではありません。"]}
    return data, metadata
