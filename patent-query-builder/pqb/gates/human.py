"""Human 決定者。

- HumanDecider: 常に PendingHuman を投げる（UI／CLI が人の判断を受けて orchestrator の
  decide_g1〜decide_g4 を直接呼ぶ運用）。
- ScriptedHumanDecider: 記録済み判断（JSON）を返す。統合テストと後方テスト用（§14.3）。
"""
from __future__ import annotations

from .base import Decider, Decision


class HumanDecider(Decider):
    actor = "human"


class ScriptedHumanDecider(Decider):
    """記録済み判断。無い項目は「提案をそのまま受け入れる」既定動作。"""
    actor = "human"

    def __init__(self, script: dict | None = None):
        self.script = script or {}
        self.g4_calls = 0

    def g1(self, context: dict) -> Decision:
        axes = self.script.get("G1", {}).get("axes") or context.get("axes") or []
        return Decision("G1", self.actor, "fix_axes", {"axes": axes})

    def g2(self, context: dict) -> Decision:
        spec = self.script.get("G2", {})
        decisions = []
        rules = spec.get("rules") or {}
        adopt_terms = set(spec.get("adopt_terms") or [])
        reject_terms = set(spec.get("reject_terms") or [])
        adopt_codes = set(spec.get("adopt_codes") or [])
        for c in context.get("candidates", []):
            if c["status"] != "candidate":
                continue
            if c["kind"] == "term":
                rule = rules.get("terms", "adopt_input")
                origin = c.get("origin", "")
                if c["value"] in reject_terms:
                    status = "rejected"
                elif c["value"] in adopt_terms or rule == "adopt_all":
                    status = "adopted"
                elif rule == "adopt_input":
                    status = "adopted" if origin.startswith(("input", "seed", "human")) else "rejected"
                elif rule == "adopt_input_and_rsj":
                    ok = origin.startswith(("input", "seed", "human")) or (origin.startswith("rsj") and (c.get("rsj_w") or 0) > 0 and not c.get("needs_review"))
                    status = "adopted" if ok else "rejected"
                else:
                    status = "rejected"
            else:
                key = f"{c['scheme']}:{c['value']}"
                if key in adopt_codes or rules.get("codes", "adopt_known") == "adopt_all":
                    status = "adopted"
                elif rules.get("codes", "adopt_known") == "adopt_known":
                    status = "adopted" if c.get("dict_known") else "rejected"
                else:
                    status = "rejected"
            decisions.append({"candidate_id": c["candidate_id"], "status": status, "reason_code": "01" if status == "adopted" else "08"})
        return Decision("G2", self.actor, "decide_candidates",
                        {"decisions": decisions, "extra": spec.get("extra") or {}, "broad_drop_axis": spec.get("broad_drop_axis")})

    def g3(self, context: dict) -> Decision:
        spec = self.script.get("G3", {})
        return Decision("G3", self.actor, "runs", {"mode": spec.get("mode", "local_index"), "runs": spec.get("runs") or []})

    def g4(self, context: dict) -> Decision:
        self.g4_calls += 1
        spec = self.script.get("G4", {})
        actions = spec.get("actions") or ["finalize"]
        action = actions[min(self.g4_calls, len(actions)) - 1]
        adopt_ops = set(spec.get("adopt_ops") or [])
        transforms = []
        for t in context.get("transforms", []):
            if t["status"] != "candidate":
                continue
            ok = t["op"] in adopt_ops or (spec.get("adopt_local_improvements") and t.get("local_eval")
                                          and not t.get("regression") and (t.get("pred_recall_pool") or 0) >= 1.0)
            transforms.append({"transform_id": t["transform_id"], "status": "adopted" if ok else "rejected"})
        return Decision("G4", self.actor, action,
                        {"judgments": spec.get("judgments") or [], "transforms": transforms, "action": action})
