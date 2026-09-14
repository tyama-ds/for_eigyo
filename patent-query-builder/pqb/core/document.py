"""文献（DB の 1 レコード）。DB アダプタ・局所照合・統計が共有する最小のデータ型。"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMES = ("FI", "FT", "IPC")
FIELDS = ("TI", "AB", "CL", "TX")


@dataclass
class Document:
    doc_id: str
    title: str = ""
    abstract: str = ""
    claims: str = ""
    codes: dict = field(default_factory=lambda: {"FI": [], "FT": [], "IPC": []})
    pub_date: str = ""
    applicant: str = ""
    citations: list = field(default_factory=list)
    rank: int | None = None

    def __post_init__(self) -> None:
        codes = {s: [] for s in SCHEMES}
        for scheme, values in (self.codes or {}).items():
            if scheme in codes and values:
                codes[scheme] = [str(v) for v in values if str(v).strip()]
        self.codes = codes

    def text(self, field_name: str) -> str:
        if field_name == "TI":
            return self.title
        if field_name == "AB":
            return self.abstract
        if field_name == "CL":
            return self.claims
        if field_name == "TX":
            return "\n".join(x for x in (self.title, self.abstract, self.claims) if x)
        raise KeyError(field_name)

    def full_text(self) -> str:
        return self.text("TX")

    def all_codes(self) -> list[tuple[str, str]]:
        return [(s, c) for s in SCHEMES for c in self.codes.get(s, [])]

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id, "title": self.title, "abstract": self.abstract,
            "claims": self.claims, "codes": self.codes, "pub_date": self.pub_date,
            "applicant": self.applicant, "citations": list(self.citations), "rank": self.rank,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Document":
        return cls(
            doc_id=str(d.get("doc_id") or d.get("id") or ""),
            title=str(d.get("title") or ""), abstract=str(d.get("abstract") or ""),
            claims=str(d.get("claims") or ""), codes=d.get("codes") or {},
            pub_date=str(d.get("pub_date") or ""), applicant=str(d.get("applicant") or ""),
            citations=list(d.get("citations") or []), rank=d.get("rank"),
        )
