"""検索式 DSL（DB 方言に依存しない JSON）。企画書 §9.2。

- Query は複数の Block（観点）を持つ。Block 内の語は OR、コードは OR、
  語群とコード群の結合は term_code_join（OR／AND）。Block 間は AND。
- status（candidate／adopted／rejected）を持ち、rejected も履歴として DSL に残す。
  レンダリング・照合には adopted のみを使う。
- 検索式文字列は LLM に書かせない。DSL からレンダラで決定的に生成する（§5.4）。
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field

from .document import FIELDS, SCHEMES

DSL_VERSION = "1.0"
STATUSES = ("candidate", "adopted", "rejected")
VARIANTS = ("broad", "standard", "narrow")
JOINS = ("OR", "AND")


def order_fields(fields) -> list[str]:
    """フィールドを固定順（TI, AB, CL, TX）に並べ、重複と未知を除く。"""
    seen = [f for f in FIELDS if f in set(fields or [])]
    return seen


@dataclass
class Term:
    text: str
    fields: list = field(default_factory=lambda: ["AB", "CL"])
    origin: str = "input"
    status: str = "adopted"
    note: str = ""

    def __post_init__(self) -> None:
        self.text = str(self.text).strip()
        self.fields = order_fields(self.fields) or ["AB", "CL"]


@dataclass
class Code:
    scheme: str
    code: str
    level: str = ""
    origin: str = "input"
    status: str = "adopted"
    title: str = ""

    def __post_init__(self) -> None:
        self.scheme = str(self.scheme).upper()
        self.code = str(self.code).strip()


@dataclass
class Block:
    axis_id: str
    axis_name: str = ""
    required: bool = True
    terms: list = field(default_factory=list)
    codes: list = field(default_factory=list)
    term_code_join: str = "OR"
    proximity: int | None = None

    def __post_init__(self) -> None:
        self.terms = [t if isinstance(t, Term) else Term(**t) for t in self.terms]
        self.codes = [c if isinstance(c, Code) else Code(**c) for c in self.codes]

    def adopted_terms(self) -> list:
        return [t for t in self.terms if t.status == "adopted" and t.text]

    def adopted_codes(self) -> list:
        return [c for c in self.codes if c.status == "adopted" and c.code]

    def is_empty(self) -> bool:
        return not self.adopted_terms() and not self.adopted_codes()

    def effective_join(self) -> str:
        """語群とコード群の両方が無い場合は OR と同義。"""
        if self.adopted_terms() and self.adopted_codes():
            return self.term_code_join
        return "OR"


@dataclass
class DateRange:
    from_: str | None = None
    to: str | None = None
    basis: str = "publication"

    def to_dict(self) -> dict:
        return {"from": self.from_, "to": self.to, "basis": self.basis}

    @classmethod
    def from_dict(cls, d) -> "DateRange":
        d = d or {}
        return cls(from_=d.get("from"), to=d.get("to"), basis=d.get("basis") or "publication")


@dataclass
class Query:
    query_id: str = ""
    case_id: str = ""
    purpose: str = "prior_art"
    variant: str = "standard"
    blocks: list = field(default_factory=list)
    exclusions: list = field(default_factory=list)
    date_range: DateRange = field(default_factory=DateRange)
    countries: list = field(default_factory=lambda: ["JP"])
    provenance: dict = field(default_factory=dict)
    dsl_version: str = DSL_VERSION

    def __post_init__(self) -> None:
        self.blocks = [b if isinstance(b, Block) else Block(**b) for b in self.blocks]
        self.exclusions = [b if isinstance(b, Block) else Block(**b) for b in self.exclusions]
        if not isinstance(self.date_range, DateRange):
            self.date_range = DateRange.from_dict(self.date_range)

    # ------------------------------------------------------------ 検証
    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.variant not in VARIANTS:
            errors.append(f"variant が不正です: {self.variant}")
        if not self.active_blocks():
            errors.append("採用済みの語・コードを持つ Block がありません")
        seen_axes: set[str] = set()
        for b in self.blocks:
            if b.axis_id in seen_axes:
                errors.append(f"axis_id が重複しています: {b.axis_id}")
            seen_axes.add(b.axis_id)
            if b.term_code_join not in JOINS:
                errors.append(f"term_code_join が不正です: {b.axis_id}={b.term_code_join}")
            for t in b.terms:
                if t.status not in STATUSES:
                    errors.append(f"status が不正です: {t.text}={t.status}")
                if not t.fields:
                    errors.append(f"fields が空です: {t.text}")
            for c in b.codes:
                if c.scheme not in SCHEMES:
                    errors.append(f"scheme が不正です: {c.scheme}")
                if c.status not in STATUSES:
                    errors.append(f"status が不正です: {c.code}={c.status}")
        return errors

    # ------------------------------------------------------------ 参照
    def active_blocks(self) -> list:
        return [b for b in self.blocks if not b.is_empty()]

    def active_exclusions(self) -> list:
        return [b for b in self.exclusions if not b.is_empty()]

    def block(self, axis_id: str) -> Block | None:
        for b in self.blocks:
            if b.axis_id == axis_id:
                return b
        return None

    def complexity(self) -> int:
        """語数＋コード数＋Block 数（§10.6）。"""
        n = 0
        for b in self.active_blocks():
            n += len(b.adopted_terms()) + len(b.adopted_codes()) + 1
        return n

    def signature(self) -> tuple:
        """構造の同一性（往復テスト用）。adopted の語・コードと結合のみを見る。"""
        def block_sig(b: Block) -> tuple:
            terms = tuple(sorted((t.text, tuple(t.fields)) for t in b.adopted_terms()))
            codes = tuple(sorted((c.scheme, c.code) for c in b.adopted_codes()))
            return (b.effective_join(), terms, codes)
        return (tuple(block_sig(b) for b in self.active_blocks()),
                tuple(block_sig(b) for b in self.active_exclusions()))

    # ------------------------------------------------------------ 変換
    def copy(self) -> "Query":
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return {
            "dsl_version": self.dsl_version,
            "query_id": self.query_id,
            "case_id": self.case_id,
            "purpose": self.purpose,
            "variant": self.variant,
            "blocks": [asdict(b) for b in self.blocks],
            "exclusions": [asdict(b) for b in self.exclusions],
            "date_range": self.date_range.to_dict(),
            "countries": list(self.countries),
            "provenance": dict(self.provenance),
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "Query":
        return cls(
            query_id=d.get("query_id", ""), case_id=d.get("case_id", ""),
            purpose=d.get("purpose", "prior_art"), variant=d.get("variant", "standard"),
            blocks=d.get("blocks") or [], exclusions=d.get("exclusions") or [],
            date_range=DateRange.from_dict(d.get("date_range")),
            countries=list(d.get("countries") or ["JP"]),
            provenance=dict(d.get("provenance") or {}),
            dsl_version=d.get("dsl_version", DSL_VERSION),
        )

    @classmethod
    def from_json(cls, text: str) -> "Query":
        return cls.from_dict(json.loads(text))
