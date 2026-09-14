"""ゲート（G1〜G4）と決定者の共通インタフェース。企画書 §5.3・§8.1。

各ゲートは decide(context) -> Decision を持つ。Human 実装は UI／Excel／スクリプトで人が入力し、
Policy 実装はルールと閾値で判断する。案Aと案Bの差はこの差し替えだけ。
"""
from __future__ import annotations

from dataclasses import dataclass, field

GATES = ("G1", "G2", "G3", "G4")


@dataclass
class Decision:
    gate: str
    actor: str                  # human | policy
    action: str                 # fix_axes | decide_candidates | runs | finalize | iterate | ...
    payload: dict = field(default_factory=dict)


class PendingHuman(Exception):
    """人の判断待ち。UI／CLI はここで止まり、判断を受け取ってから続行する。"""

    def __init__(self, gate: str, context: dict):
        super().__init__(f"{gate}: 人の判断待ち")
        self.gate, self.context = gate, context


class Decider:
    """決定者の基底。ゲートごとのメソッドをオーバーライドする。"""
    actor = "human"

    def g1(self, context: dict) -> Decision:
        raise PendingHuman("G1", context)

    def g2(self, context: dict) -> Decision:
        raise PendingHuman("G2", context)

    def g3(self, context: dict) -> Decision:
        raise PendingHuman("G3", context)

    def g4(self, context: dict) -> Decision:
        raise PendingHuman("G4", context)

    def decide(self, gate: str, context: dict) -> Decision:
        return {"G1": self.g1, "G2": self.g2, "G3": self.g3, "G4": self.g4}[gate](context)
