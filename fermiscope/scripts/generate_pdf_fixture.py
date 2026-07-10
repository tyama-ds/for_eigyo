"""テスト・デモ用の最小PDFフィクスチャを生成する。

外部依存なしで正しいxrefを持つPDF/1.4を手書き生成する。
(日本語テキストはCIDフォント埋め込みが必要になるため、PDFは英語とする)
"""

from __future__ import annotations

from pathlib import Path

LINES = [
    "Piano Technician Workload Survey 2024",
    "Publisher: Piano Technicians Research Panel",
    "Published: 2024-06-30",
    "Method: mail survey of 1,200 piano technicians nationwide, 2024",
    "Region: Japan",
    "Period: 2024",
    "",
    "Key findings:",
    "Average workload: 1.5 tunings per technician per day (range 1.0 - 2.0).",
    "Annual working days: 200 days per year (range 180 - 220).",
    "Full-time technicians report about 600 tunings per year,",
    "while part-time technicians report about 150 tunings per year.",
]


def build_pdf(lines: list[str]) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    content_parts = ["BT /F1 11 Tf 50 760 Td 16 TL"]
    for i, line in enumerate(lines):
        if i > 0:
            content_parts.append("T*")
        content_parts.append(f"({esc(line)}) Tj")
    content_parts.append("ET")
    content = "\n".join(content_parts).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


if __name__ == "__main__":
    target = (
        Path(__file__).resolve().parent.parent
        / "src/fermiscope/data/mock_corpus/documents/tuner_workload.pdf"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_pdf(LINES))
    print(f"wrote {target} ({target.stat().st_size} bytes)")
