"""Policy 決定者（案A: 完全自動）。企画書 §7.2・§7.3。ルールと閾値はすべて config から。"""
from __future__ import annotations

from .base import Decider, Decision


class PolicyDecider(Decider):
    actor = "policy"

    def __init__(self, cfg: dict, purpose: dict):
        self.cfg = cfg
        self.purpose = purpose
        self.flip = float(cfg.get("flip_threshold", 0.2))
        self.rsj_cfg = cfg.get("rsj") or {}

    # G1: P1 の提案をそのまま採用（必須観点が無ければ最初の観点を必須にする）
    def g1(self, context: dict) -> Decision:
        axes = [dict(a) for a in context.get("axes", [])]
        if axes and not any(a.get("kind") == "required" for a in axes):
            axes[0]["kind"] = "required"
        return Decision("G1", self.actor, "fix_axes", {"axes": axes})

    # G2: 語は由来と確信度、コードは辞書照合とフリップ率で採否
    def g2(self, context: dict) -> Decision:
        decisions = []
        top_terms = int(self.rsj_cfg.get("top_terms", 30))
        n_rsj_terms = 0
        for c in sorted(context.get("candidates", []), key=lambda x: -(x.get("offer_w") or 0)):
            if c["status"] != "candidate":
                continue
            origin = c.get("origin") or ""
            if c["kind"] == "term":
                if origin.startswith(("input", "seed", "human")):
                    status = "adopted"
                elif origin.startswith("rsj") or origin.startswith("tree") or origin.startswith("cal"):
                    ok = (c.get("rsj_w") or 0) > 0 and not c.get("needs_review") and n_rsj_terms < max(5, top_terms // 3)
                    status = "adopted" if ok else "rejected"
                    n_rsj_terms += int(ok)
                else:  # llm / dict
                    conf = c.get("confidence")
                    kind = c.get("variant_kind") or ""
                    ok = (conf is None or conf >= 0.6) and kind in ("synonym", "variant", "abbreviation", "english", "original", "")
                    status = "adopted" if ok else "rejected"
            else:
                known = c.get("dict_known")
                flip = c.get("flip_rate")
                ok = bool(known) and (flip is None or flip <= self.flip) and not c.get("needs_review")
                if ok and origin.startswith("rsj"):
                    ok = (c.get("rsj_w") or 0) > 0
                status = "adopted" if ok else "rejected"
            decisions.append({"candidate_id": c["candidate_id"], "status": status,
                              "reason_code": "01" if status == "adopted" else "08", "note": "policy"})
        return Decision("G2", self.actor, "decide_candidates", {"decisions": decisions, "extra": {}, "broad_drop_axis": None})

    # G3: DB アダプタ（local_index／api）で 3 案を実行
    def g3(self, context: dict) -> Decision:
        return Decision("G3", self.actor, "runs", {"mode": (self.cfg.get("db") or {}).get("mode") or "local_index"})

    # G4: LLM 採点を受け入れ、パレート改善の変換だけ採用。停止条件で確定
    def g4(self, context: dict) -> Decision:
        stop = context.get("stop") or {}
        per_iter = int((self.cfg.get("budget") or {}).get("db_runs_per_iteration") or 3)
        transforms, n_widen = [], 0
        base = context.get("base_metrics") or {}
        for t in context.get("transforms", []):
            if t["status"] != "candidate":
                continue
            ok = False
            if t.get("regression"):
                ok = False
            elif t.get("local_eval"):
                # 狭める: プール再現率を保ち、母集団を小さくする（パレート改善）
                keeps_pool = (t.get("pred_recall_pool") is None) or (t["pred_recall_pool"] >= (base.get("recall_pool") or 0))
                smaller = (t.get("pred_hits") is not None) and (base.get("hit_count") is not None) and t["pred_hits"] < base["hit_count"]
                ok = keeps_pool and smaller
            else:
                # 広げる: 上限件数まで、統計由来かつ観点割当が確定しているものだけ
                auto = (t.get("target") or {}).get("assigned") == "auto"
                if n_widen < per_iter and t.get("source") in ("rsj", "tree") and not auto:
                    ok = True
                    n_widen += 1
            transforms.append({"transform_id": t["transform_id"], "status": "adopted" if ok else "rejected"})
        action = "finalize" if stop.get("should_stop") else "iterate"
        if not any(t["status"] == "adopted" for t in transforms):
            action = "finalize"   # 変換が無ければ再反復しても変わらない
        return Decision("G4", self.actor, action, {"judgments": [], "transforms": transforms, "action": action,
                                                   "stop": stop})
