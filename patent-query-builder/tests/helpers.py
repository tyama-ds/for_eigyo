"""テスト共通: アプリのパスを通し、標準ライブラリのみで動く小さなフィクスチャを作る。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from pqb.core.document import Document  # noqa: E402
from pqb.core.dsl import Block, Code, Query, Term  # noqa: E402

SAMPLE_DIR = APP_DIR / "sample_data" / "case_0001"


def sample_query(join: str = "OR") -> Query:
    return Query(query_id="CASE-T/v1/standard", case_id="CASE-T", variant="standard", blocks=[
        Block(axis_id="A", axis_name="対象物", terms=[Term("高強度鋼板", fields=["TI", "AB", "CL"]), Term("高張力鋼板", fields=["TI", "AB", "CL"], origin="llm:P2"),
                                                   Term("旧語", status="rejected")],
              codes=[Code("FI", "C22C38/00", origin="seed:JP1")], term_code_join=join),
        Block(axis_id="B", axis_name="手段", terms=[Term("焼入れ", fields=["TI", "AB", "CL"])]),
    ])


def toy_docs(n: int = 60) -> tuple[list[Document], dict[str, bool]]:
    import random
    rng = random.Random(1)
    docs, rel = [], {}
    for i in range(n):
        r = i % 3 == 0
        if r:
            t = rng.choice(["高強度鋼板の焼入れ方法", "高張力鋼板のホットスタンプ部材", "ハイテン材の急冷処理"])
            ab, codes = "自動車部材に用いる鋼板を加熱後に焼入れする。", {"FI": ["C22C38/04", "C21D9/46"], "FT": ["4K037AA01"]}
        elif i % 3 == 1:
            t = rng.choice(["鋼板の圧延方法", "めっき鋼板の製造", "溶接鋼管"])
            ab, codes = "鋼板を圧延しめっきする。", {"FI": ["C21D8/02", "C23C2/06"], "FT": ["4K027AA01"]}
        else:
            t = rng.choice(["アルミニウム合金の熱処理", "樹脂成形品", "リチウム電池"])
            ab, codes = "アルミ合金を焼入れする。", {"FI": ["C22F1/04"], "FT": ["4K018AA01"]}
        d = Document(f"D{i:03d}", title=t, abstract=ab, codes=codes, rank=i + 1)
        docs.append(d)
        rel[d.doc_id] = r
    return docs, rel


def sample_script() -> dict:
    return json.loads((SAMPLE_DIR / "decisions.json").read_text(encoding="utf-8"))
