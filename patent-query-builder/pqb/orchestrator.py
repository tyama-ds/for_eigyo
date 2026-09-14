"""6 段パイプラインの実行（案B: semi ／ 案A: auto）。企画書 §5.2・§8・§11。

状態遷移（cases.status）
  new → g1_pending →（G1）→ axes_fixed → g2_pending →（G2）→ g3_pending →（G3: 実行・取り込み）
      → 採点（P4）→ 分析（RSJ・決定木・変換・推定）→ g4_pending →（G4）→ finalized ／ 再反復（→ g2_pending）

UI／CLI は各ステップのメソッドを直接呼ぶ（人が決定者）。run_case() は Decider を差し替えて
同じ手順を自動で回す（ScriptedHuman: テスト・後方テスト、Policy: 案A）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config as cfgmod
from .core.document import Document
from .core.dsl import Block, Code, Query, Term
from .core.match import match_ids
from .core.render import Dialect, RenderError, render_parts, roundtrip_ok
from .core.variants import build_variants
from .db.adapter import DBAdapter, DBError
from .eval.metrics import evaluate, in_range, pareto_front, satisfies_constraints, select_by_policy
from .eval.sampling import draw_sample, estimate_recall
from .gates.base import Decision, PendingHuman
from .knowledge import codes as codelib
from .knowledge.codes import CodeDictionary
from .learn.boolean import tree_transforms
from .learn.cal import cal_round
from .learn.transforms import (Transform, dedupe_transforms, direction_of, local_evaluate, propose_from_stats,
                               summarize_query, ALL_OPS)
from .llm.adapter import LLMAdapter, LLMError, Masker, code_support
from .stats.rsj import rank_candidates
from .stats.tokenize import load_stopwords
from .store.db import Store
from .util import new_id, norm_text, now_iso

SAMPLE_DIR = cfgmod.BASE / "sample_data" / "case_0001"
VARIANTS = ("broad", "standard", "narrow")


class OrchestratorError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, store: Store, cfg: dict | None = None, prompts_dir: Path | None = None):
        self.store = store
        self.cfg = cfg or cfgmod.load_config()
        self.prompts_dir = prompts_dir
        self.stopwords = load_stopwords()
        self.dictionary = CodeDictionary()
        self.dictionary.load_rows(self.store.dictionary_code_rows())

    # ================================================================ 補助
    def reload_config(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or cfgmod.load_config()

    def _case(self, case_id: str) -> dict:
        case = self.store.get_case(case_id)
        if case is None:
            raise OrchestratorError(f"案件がありません: {case_id}")
        return case

    def _llm(self, case: dict) -> LLMAdapter:
        settings = case.get("settings") or {}
        mcfg = (self.cfg.get("llm") or {}).get("masking") or {}
        masker = None
        if mcfg.get("enabled") or settings.get("mask_terms"):
            masker = Masker(case["case_id"], self.store, settings.get("mask_terms") or [], bool(mcfg.get("mask_numbers")))
        return LLMAdapter(self.cfg, self.store, case["case_id"], masker=masker,
                          prompts_dir=self.prompts_dir if self.prompts_dir is not None else cfgmod.data_dir())

    def _db(self) -> DBAdapter:
        return DBAdapter(self.cfg, self.store)

    def _purpose(self, case: dict) -> dict:
        return cfgmod.purpose_settings(case.get("purpose", "prior_art"))

    def _dialect_ids(self, case: dict) -> list[str]:
        ids = list(self.cfg.get("dialects") or ["jplatpat", "generic"])
        main = case.get("dialect")
        if main and main not in ids:
            ids.insert(0, main)
        return ids

    def _log(self, case_id: str, gate: str, actor: str, action: str, message: str = "") -> None:
        case = self.store.get_case(case_id)
        self.store.log(case_id, case["iteration"] if case else 0, gate, actor, action, message)

    def _save_settings(self, case_id: str, **updates) -> dict:
        case = self._case(case_id)
        settings = dict(case.get("settings") or {})
        settings.update(updates)
        self.store.update_case(case_id, settings=settings)
        return settings

    def _observe_codes(self, docs) -> None:
        for d in docs:
            for scheme, code in d.all_codes():
                self.dictionary.observe(scheme, code)
        rows = [r for r in self.dictionary.rows() if r["origin"] == "observed"]
        if rows:
            self.store.upsert_dictionary_codes(rows)

    def import_code_dictionary(self, csv_text: str) -> int:
        n = self.dictionary.load_csv_text(csv_text, origin="official")
        self.store.upsert_dictionary_codes(self.dictionary.rows())
        return n

    def load_local_index(self, docs, replace: bool = False) -> int:
        self._observe_codes(docs)
        return self.store.load_local_index(docs, replace=replace)

    # ================================================================ 案件
    def create_case(self, *, name: str, purpose: str, input_text: str, seeds=None, date_from: str = "",
                    date_to: str = "", countries=None, dialect: str | None = None, csv_dialect: str | None = None,
                    settings: dict | None = None, case_id: str | None = None, actor: str = "human") -> dict:
        if not input_text.strip():
            raise OrchestratorError("入力文（技術説明）が空です")
        if not case_id:
            n = len(self.store.list_cases()) + 1
            case_id = f"CASE-{n:04d}"
            while self.store.get_case(case_id):
                n += 1
                case_id = f"CASE-{n:04d}"
        seed_ids: list[str] = []
        seed_docs: list[Document] = []
        for s in seeds or []:
            if isinstance(s, dict):
                d = Document.from_dict(s)
                if d.doc_id:
                    seed_docs.append(d)
                    seed_ids.append(d.doc_id)
            elif str(s).strip():
                seed_ids.append(codelib.normalize("", str(s)))
        if seed_docs:
            self.store.upsert_documents(seed_docs)
            self._observe_codes(seed_docs)
        case = self.store.create_case(case_id=case_id, name=name or case_id, purpose=purpose or "prior_art",
                                      input_text=input_text, seeds=seed_ids, countries=countries or ["JP"],
                                      date_from=date_from, date_to=date_to,
                                      dialect=dialect or self.cfg.get("default_dialect") or "jplatpat",
                                      csv_dialect=csv_dialect or self.cfg.get("csv_dialect") or "jplatpat",
                                      settings=settings or {}, actor=actor)
        if seed_ids:
            self.store.add_to_pool(case_id, seed_ids, "seed", 1)
            for sid in seed_ids:
                self.store.add_judgment(case_id=case_id, doc_id=sid, iteration=1, selection="seed", judge="seed",
                                        overall=3, rationale="既知文献（依頼時点で適合）")
        self._log(case_id, "-", actor, "create_case", f"purpose={purpose} seeds={len(seed_ids)}")
        return case

    def delete_case(self, case_id: str) -> None:
        self._case(case_id)
        self.store.delete_case(case_id)

    def load_sample_case(self, actor: str = "human") -> dict:
        """同梱サンプル（合成データ）で案件を作り、母集団を local_index に読み込む。"""
        from .db.csv_import import import_csv_bytes
        input_text = (SAMPLE_DIR / "input.txt").read_text(encoding="utf-8")
        seeds = json.loads((SAMPLE_DIR / "seeds.json").read_text(encoding="utf-8"))
        self.import_code_dictionary((SAMPLE_DIR / "codes_subset.csv").read_text(encoding="utf-8"))
        docs, _ = import_csv_bytes((SAMPLE_DIR / "population.csv").read_bytes(), cfgmod.load_csv_dialect("jplatpat"))
        self.load_local_index(docs)
        case = self.create_case(name="サンプル: 高強度鋼板の焼入れ（合成データ）", purpose="prior_art", input_text=input_text,
                                seeds=seeds, date_from="2008-01-01", actor=actor,
                                settings={"sample": True, "note": "合成データ。実データではない"})
        return case

    # ================================================================ 段 1: 構造化 → G1
    def structure(self, case_id: str, confirmed: bool = False, actor: str = "system") -> dict:
        case = self._case(case_id)
        seed_docs = self.store.get_documents(case["seeds"])
        inputs = {"input_text": case["input_text"], "purpose_label": self._purpose(case).get("label", case["purpose"]),
                  "seed_docs": [{"doc_id": d.doc_id, "title": d.title} for d in seed_docs.values()]}
        result = self._llm(case).complete("P1", inputs, confirmed=confirmed)
        axes = self._normalize_axes(result.majority.get("axes") or [])
        for a in axes:
            a["origin"] = "llm:P1"
        self.store.replace_axes(case_id, axes, actor="llm")
        self.store.update_case(case_id, status="g1_pending")
        self._save_settings(case_id, p1={"low_confidence": result.low_confidence, "notes": result.majority.get("notes", ""),
                                         "mode": result.mode})
        self._log(case_id, "G1", "llm", "structure", f"axes={len(axes)} mode={result.mode}")
        return {"axes": axes, "llm": {"mode": result.mode, "flip_rate": result.flip_rate, "low_confidence": result.low_confidence,
                                      "notes": result.majority.get("notes", "")}}

    @staticmethod
    def _normalize_axes(axes: list[dict]) -> list[dict]:
        out, seen = [], set()
        for i, a in enumerate(axes):
            aid = str(a.get("axis_id") or chr(65 + i)).strip().upper()[:4] or chr(65 + i)
            while aid in seen:
                aid += "2"
            seen.add(aid)
            terms = [str(t).strip() for t in (a.get("terms") or []) if str(t).strip()]
            out.append({"axis_id": aid, "name": str(a.get("name") or aid).strip(), "kind": a.get("kind") if a.get("kind") in ("required", "auxiliary") else "required",
                        "category": a.get("category", "other"), "definition": str(a.get("definition") or ""),
                        "evidence": str(a.get("evidence") or ""), "terms": terms, "origin": a.get("origin", "human"),
                        "fixed": bool(a.get("fixed"))})
        return out

    def decide_g1(self, case_id: str, axes: list[dict], actor: str = "human") -> dict:
        case = self._case(case_id)
        proposed = {a["axis_id"]: a for a in self.store.get_axes(case_id)}
        for a in axes:
            if not a.get("terms") and a.get("axis_id") in proposed:
                a["terms"] = proposed[a["axis_id"]].get("terms") or []
        axes = self._normalize_axes(axes)
        if not axes:
            raise OrchestratorError("観点が 1 つもありません")
        if not any(a["kind"] == "required" for a in axes):
            raise OrchestratorError("必須観点が 1 つ以上必要です")
        for a in axes:
            a["fixed"] = True
            a["origin"] = a.get("origin") or actor
        self.store.replace_axes(case_id, axes, actor=actor)
        it = case["iteration"]
        for a in axes:
            for t in a.get("terms") or []:
                if not self.store.find_candidate(case_id, "term", a["axis_id"], t):
                    self.store.add_candidate(case_id, {"kind": "term", "axis_id": a["axis_id"], "value": t, "variant_kind": "original",
                                                       "origin": "input", "status": "adopted", "iteration": it,
                                                       "decided_by": actor, "decided_at": now_iso(), "reason_code": "01"}, actor=actor)
        self.store.add_decision(case_id, it, "G1", actor, "fix_axes", {"axes": axes})
        self.store.update_case(case_id, status="axes_fixed")
        self._log(case_id, "G1", actor, "fix_axes", ", ".join(f"{a['axis_id']}:{a['name']}({a['kind']})" for a in axes))
        return {"axes": self.store.get_axes(case_id)}

    # ================================================================ 段 2: 展開 → G2
    def _axis_terms(self, case_id: str, status: str | None = "adopted") -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for c in self.store.get_candidates(case_id, kind="term", status=status):
            out.setdefault(c["axis_id"], []).append(c["value"])
        return out

    def _pool_docs(self, case_id: str) -> list[Document]:
        ids = sorted(self.store.pool_ids(case_id))
        docs = self.store.get_documents(ids)
        return [docs[i] for i in ids if i in docs]

    def expand(self, case_id: str, confirmed: bool = False, actor: str = "system") -> dict:
        case = self._case(case_id)
        axes = self.store.get_axes(case_id)
        if not axes or not any(a["fixed"] for a in axes):
            raise OrchestratorError("先に G1 で観点を確定してください")
        it = case["iteration"]
        terms_by_axis = self._axis_terms(case_id)
        pool_docs = self._pool_docs(case_id)
        flip_thr = float(self.cfg.get("flip_threshold", 0.2))
        llm = self._llm(case)
        added = {"terms": 0, "codes": 0, "rsj_terms": 0, "rsj_codes": 0}
        info: dict = {}

        # --- 語: 辞書
        hints: dict[str, list[str]] = {}
        for aid, terms in terms_by_axis.items():
            for t in terms:
                syns = [r["synonym"] for r in self.store.term_synonyms(t) if r["adopted"] > r["rejected"]]
                if syns:
                    hints[t] = syns
                for s in syns:
                    if self._add_term_candidate(case_id, aid, s, "synonym", "dict", "", 0.8, it):
                        added["terms"] += 1
        # --- 語: P2
        axes_for_llm = [{"axis_id": a["axis_id"], "name": a["name"], "kind": a["kind"], "definition": a["definition"],
                         "terms": terms_by_axis.get(a["axis_id"], [])} for a in axes]
        rel_docs = [{"doc_id": d.doc_id, "title": d.title, "abstract": d.abstract[:400]} for d in pool_docs[:30]]
        r2 = llm.complete("P2", {"axes": axes_for_llm, "terms_by_axis": terms_by_axis, "relevant_docs": rel_docs,
                                 "synonyms_hint": hints}, confirmed=confirmed)
        valid_axes = {a["axis_id"] for a in axes}
        known_doc_ids = {d.doc_id for d in pool_docs}
        for t in r2.majority.get("terms", []):
            aid = t.get("axis_id")
            if aid not in valid_axes:
                continue
            origin = str(t.get("origin") or "input")
            origin_doc = ""
            if origin.startswith("doc:"):
                origin_doc = origin[4:]
                if origin_doc not in known_doc_ids:      # 入力に無い文献番号は棄却（幻覚対策）
                    continue
                origin = "llm:P2"
            elif origin == "dict":
                origin = "dict"
            else:
                origin = "llm:P2"
            if self._add_term_candidate(case_id, aid, t["text"], t.get("variant_kind", "synonym"), origin, origin_doc,
                                        t.get("confidence"), it):
                added["terms"] += 1
        info["p2"] = {"mode": r2.mode, "low_confidence": r2.low_confidence, "n": len(r2.majority.get("terms", []))}

        # --- 分類: 既知文献の集計
        code_counts: dict[str, dict[str, int]] = {}
        for d in pool_docs:
            for scheme, code in d.all_codes():
                code = codelib.normalize(scheme, code)
                code_counts.setdefault(scheme, {})
                code_counts[scheme][code] = code_counts[scheme].get(code, 0) + 1
        seed_code_counts = {s: [{"code": c, "count": n, "title": self.dictionary.title(s, c)}
                                for c, n in sorted(cnt.items(), key=lambda kv: -kv[1])[:12]]
                            for s, cnt in code_counts.items()}
        required = [a for a in axes if a["kind"] == "required"] or axes
        # 既知文献の付与分類はそのまま候補に（第一候補）
        for scheme, items in seed_code_counts.items():
            for item in items:
                if item["count"] >= 1:
                    aid = self._axis_for_title(axes, terms_by_axis, item["title"]) or required[0]["axis_id"]
                    if self._add_code_candidate(case_id, aid, scheme, item["code"], f"seed:{item['count']}件", "", item["count"] / max(1, len(pool_docs)), it, flip=None):
                        added["codes"] += 1
        # --- 分類: P3（n サンプル → フリップ率）
        known_sample = [r for r in self.dictionary.rows() if r["origin"] == "official"][:200]
        r3 = llm.complete("P3", {"axes": axes_for_llm, "terms_by_axis": terms_by_axis, "seed_code_counts": seed_code_counts,
                                 "seed_count": len(pool_docs), "known_codes": known_sample}, confirmed=confirmed)
        support = code_support(r3.samples)
        for (aid, scheme, code), frac in support.items():
            if aid not in valid_axes or scheme not in ("FI", "FT", "IPC"):
                continue
            code_n = codelib.normalize(scheme, code)
            if not codelib.is_valid_format(scheme, code_n):
                continue
            reason = next((c.get("reason", "") for s in r3.samples for c in s.get("codes", [])
                           if c.get("code") == code and c.get("scheme") == scheme), "")
            if self._add_code_candidate(case_id, aid, scheme, code_n, "llm:P3", "", None, it, flip=round(1 - frac, 4), note=reason):
                added["codes"] += 1
        info["p3"] = {"mode": r3.mode, "flip_rate": r3.flip_rate, "n": len(support)}

        # --- 2 回目以降: RSJ 統計を候補に付ける／追加する
        if it >= 2:
            stats = self._current_stats(case)
            if stats:
                added["rsj_terms"], added["rsj_codes"] = self._attach_rsj(case, stats, it)
        # --- 辞書照合フラグ・要確認を更新
        for c in self.store.get_candidates(case_id, kind="code"):
            known = self.dictionary.known(c["scheme"], c["value"])
            needs = (c.get("flip_rate") is not None and c["flip_rate"] > flip_thr) or not known
            self.store.update_candidate_stats(c["candidate_id"], {"dict_known": known, "needs_review": needs,
                                                                   "title": self.dictionary.title(c["scheme"], c["value"]),
                                                                   "level": codelib.level_of(c["scheme"], c["value"])})
        self.store.update_case(case_id, status="g2_pending")
        self._log(case_id, "G2", "llm", "expand", json.dumps(added, ensure_ascii=False))
        return {"added": added, "info": info, "dictionary_degraded": self.dictionary.degraded}

    def _axis_for_title(self, axes: list[dict], terms_by_axis: dict, title: str) -> str | None:
        if not title:
            return None
        best, best_len = None, 0
        for a in axes:
            for t in terms_by_axis.get(a["axis_id"], []):
                for k in range(min(len(t), 6), 1, -1):
                    if any(t[i:i + k] in title for i in range(len(t) - k + 1)):
                        if k > best_len:
                            best, best_len = a["axis_id"], k
                        break
        return best

    def _add_term_candidate(self, case_id, axis_id, text, variant_kind, origin, origin_doc, confidence, iteration, **extra) -> bool:
        text = str(text).strip()
        if not text or len(text) > 60:
            return False
        for c in self.store.get_candidates(case_id, kind="term"):
            if c["axis_id"] == axis_id and norm_text(c["value"]) == norm_text(text):
                return False
        self.store.add_candidate(case_id, {"kind": "term", "axis_id": axis_id, "value": text, "variant_kind": variant_kind,
                                           "origin": origin, "origin_doc": origin_doc, "confidence": confidence,
                                           "status": "candidate", "iteration": iteration, **extra}, actor="system")
        return True

    def _add_code_candidate(self, case_id, axis_id, scheme, code, origin, origin_doc, confidence, iteration, flip=None, note="", **extra) -> bool:
        code = codelib.normalize(scheme, code)
        if not code:
            return False
        for c in self.store.get_candidates(case_id, kind="code"):
            if c["scheme"] == scheme and c["value"] == code:
                if flip is not None and c.get("flip_rate") is None:
                    self.store.update_candidate_stats(c["candidate_id"], {"flip_rate": flip})
                return False
        self.store.add_candidate(case_id, {"kind": "code", "axis_id": axis_id, "value": code, "scheme": scheme,
                                           "level": codelib.level_of(scheme, code), "title": self.dictionary.title(scheme, code),
                                           "origin": origin, "origin_doc": origin_doc, "confidence": confidence,
                                           "flip_rate": flip, "dict_known": self.dictionary.known(scheme, code),
                                           "status": "candidate", "iteration": iteration, "note": note, **extra}, actor="system")
        return True

    def _attach_rsj(self, case: dict, stats: dict, it: int) -> tuple[int, int]:
        case_id = case["case_id"]
        n_t = n_c = 0
        cands = self.store.get_candidates(case_id)
        by_term = {(c["axis_id"], norm_text(c["value"])): c for c in cands if c["kind"] == "term"}
        by_code = {(c["scheme"], c["value"]): c for c in cands if c["kind"] == "code"}
        for s in stats.get("terms", []) + stats.get("negative_terms", []):
            hit = [c for (aid, v), c in by_term.items() if v == norm_text(s["feature"])]
            if hit:
                for c in hit:
                    self.store.update_candidate_stats(c["candidate_id"], {"r": s["r"], "n": s["n"], "R": s["R"], "N": s["N"],
                                                                           "rsj_w": s["w"], "offer_w": s["ow"], "w_sample": s["w_sample"],
                                                                           "needs_review": s["needs_review"]})
            elif s["w"] > 0 and s["r"] >= 2:
                axes = self.store.get_axes(case_id)
                aid = self._axis_for_title(axes, self._axis_terms(case_id), s["feature"])
                auto_assigned = aid is None
                if auto_assigned:
                    aid = self._guess_axis_by_cooccurrence(case_id, s)
                if self._add_term_candidate(case_id, aid, s["feature"], "extracted", f"rsj:iter{it}", ",".join(s.get("docs", [])[:3]), None, it,
                                            r=s["r"], n=s["n"], R=s["R"], N=s["N"], rsj_w=s["w"], offer_w=s["ow"],
                                            w_sample=s["w_sample"], needs_review=s["needs_review"] or auto_assigned,
                                            note="観点は自動割当（要確認）" if auto_assigned else ""):
                    n_t += 1
        for s in stats.get("codes", []) + stats.get("negative_codes", []):
            c = by_code.get((s["scheme"], s["feature"]))
            if c:
                self.store.update_candidate_stats(c["candidate_id"], {"r": s["r"], "n": s["n"], "R": s["R"], "N": s["N"],
                                                                       "rsj_w": s["w"], "offer_w": s["ow"], "w_sample": s["w_sample"],
                                                                       "needs_review": s["needs_review"]})
            elif s["w"] > 0 and s["r"] >= 2:
                axes = self.store.get_axes(case_id)
                required = [a for a in axes if a["kind"] == "required"] or axes
                aid = self._axis_for_title(axes, self._axis_terms(case_id), self.dictionary.title(s["scheme"], s["feature"])) or required[0]["axis_id"]
                if self._add_code_candidate(case_id, aid, s["scheme"], s["feature"], f"rsj:iter{it}", ",".join(s.get("docs", [])[:3]), None, it,
                                            r=s["r"], n=s["n"], R=s["R"], N=s["N"], rsj_w=s["w"], offer_w=s["ow"],
                                            w_sample=s["w_sample"], needs_review=s["needs_review"]):
                    n_c += 1
        return n_t, n_c

    def _guess_axis_by_cooccurrence(self, case_id: str, stat: dict) -> str:
        axes = self.store.get_axes(case_id)
        required = [a for a in axes if a["kind"] == "required"] or axes
        return required[0]["axis_id"]

    def decide_g2(self, case_id: str, decisions: list[dict], extra: dict | None = None,
                  broad_drop_axis: str | None = None, actor: str = "human") -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        axes = {a["axis_id"]: a for a in self.store.get_axes(case_id)}
        terms_by_axis = self._axis_terms(case_id)
        cands = {c["candidate_id"]: c for c in self.store.get_candidates(case_id)}
        n_adopt = n_reject = 0
        for d in decisions or []:
            c = cands.get(d.get("candidate_id"))
            if not c:
                continue
            status = d.get("status")
            if status not in ("adopted", "rejected", "candidate"):
                continue
            self.store.decide_candidate(c["candidate_id"], status, d.get("reason_code", ""), d.get("note"), actor, d.get("axis_id"))
            if status == "adopted":
                n_adopt += 1
            elif status == "rejected":
                n_reject += 1
            # 辞書学習
            if status in ("adopted", "rejected"):
                if c["kind"] == "term":
                    base_terms = terms_by_axis.get(c["axis_id"]) or [c["value"]]
                    if c["value"] not in base_terms:
                        self.store.bump_term_dictionary(base_terms[0], c["value"], c.get("variant_kind") or "", c.get("origin") or "", status == "adopted")
                else:
                    ax = axes.get(c["axis_id"])
                    if ax:
                        self.store.bump_axis_code_dictionary(ax["name"], c["scheme"], c["value"], status == "adopted")
        extra = extra or {}
        for t in extra.get("terms") or []:
            aid = t.get("axis_id")
            if aid in axes and str(t.get("text", "")).strip():
                self._add_term_candidate(case_id, aid, t["text"], "original", "human", "", 1.0, it,
                                         status="adopted", decided_by=actor, decided_at=now_iso(), reason_code="01")
        for c in extra.get("codes") or []:
            aid = c.get("axis_id")
            if aid in axes and c.get("scheme") in ("FI", "FT", "IPC") and str(c.get("code", "")).strip():
                self._add_code_candidate(case_id, aid, c["scheme"], c["code"], "human", "", 1.0, it,
                                         status="adopted", decided_by=actor, decided_at=now_iso(), reason_code="01")
        if broad_drop_axis is not None:
            self._save_settings(case_id, broad_drop_axis=broad_drop_axis or None)
        self.store.add_decision(case_id, it, "G2", actor, "decide_candidates",
                                {"n_adopted": n_adopt, "n_rejected": n_reject, "extra": extra, "broad_drop_axis": broad_drop_axis})
        self._log(case_id, "G2", actor, "decide_candidates", f"adopted={n_adopt} rejected={n_reject}")
        return self.build_queries(case_id, actor=actor)

    # ================================================================ 段 3: 検索式組立 → G3
    def _axis_table(self, case: dict) -> tuple[list[dict], list[dict], dict]:
        settings = case.get("settings") or {}
        axes = [dict(a) for a in self.store.get_axes(case["case_id"])]
        extra_and = set(settings.get("extra_and_axes") or [])
        dropped = set(settings.get("dropped_axes") or [])
        for a in axes:
            if a["axis_id"] in extra_and:
                a["kind"] = "required"
            if a["axis_id"] in dropped:
                a["kind"] = "auxiliary"
        cands = [dict(c) for c in self.store.get_candidates(case["case_id"])]
        overrides = settings.get("fields_override") or {}
        for c in cands:
            if c["kind"] == "term" and c["axis_id"] in overrides:
                c["fields"] = overrides[c["axis_id"]]
        return axes, cands, settings

    def build_queries(self, case_id: str, actor: str = "system") -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        axes, cands, settings = self._axis_table(case)
        if not any(c["status"] == "adopted" for c in cands):
            raise OrchestratorError("採用済みの語・コードがありません（G2 で採否を決めてください）")
        prev = self.store.get_queries(case_id, it - 1) if it > 1 else {}
        parent_ids = {v: q.query_id for v, q in prev.items()}
        queries, notes = build_variants(case, axes, cands, it, fields_cfg=self.cfg.get("fields"),
                                        broad_drop_axis=settings.get("broad_drop_axis"), parent_ids=parent_ids)
        join_and = set(settings.get("join_and_axes") or [])
        proximity = settings.get("proximity") or {}
        exclusions = settings.get("exclusions") or [] if self.cfg.get("exclusions_enabled") else []
        for v, q in queries.items():
            for b in q.blocks:
                if b.axis_id in join_and and v != "narrow":
                    b.term_code_join = "AND"
                if b.axis_id in proximity:
                    b.proximity = int(proximity[b.axis_id])
            for i, ex in enumerate(exclusions):
                q.exclusions.append(Block(axis_id=f"X{i + 1}", axis_name="除外", required=False,
                                          terms=[Term(text=t["text"], fields=t.get("fields") or ["AB", "CL"], origin="human") for t in ex.get("terms", [])],
                                          codes=[Code(scheme=c["scheme"], code=c["code"], origin="human") for c in ex.get("codes", [])]))
        out = {}
        warnings = []
        for v, q in queries.items():
            errors = q.validate()
            if errors:
                warnings.append(f"{v}: " + "; ".join(errors))
                continue
            self.store.save_query(case_id, q, actor=actor)
            rend = {}
            for did in self._dialect_ids(case):
                d = Dialect.from_dict(cfgmod.load_dialect(did))
                try:
                    parts = render_parts(q, d)
                    ok = roundtrip_ok(q, d)
                except RenderError as e:
                    warnings.append(f"{v}/{did}: {e}")
                    continue
                if not ok:
                    warnings.append(f"{v}/{did}: 往復検証 NG")
                self.store.save_renderings(q.query_id, did, parts, ok)
                rend[did] = {"parts": parts, "roundtrip_ok": ok}
            out[v] = {"query_id": q.query_id, "dsl": q.to_dict(), "renderings": rend}
        if not out:
            raise OrchestratorError("検索式を組めませんでした: " + " / ".join(warnings))
        self.store.update_case(case_id, status="g3_pending")
        self._save_settings(case_id, variant_notes=notes)
        self._log(case_id, "G3", actor, "build_queries", f"variants={list(out)} drop={notes.get('broad_drop_axis')} warnings={warnings}")
        return {"queries": out, "notes": notes, "warnings": warnings}

    def import_run(self, case_id: str, variant: str, *, data: bytes | None = None, text: str | None = None,
                   docs: list[Document] | None = None, hit_count: int | None = None, dialect: str | None = None,
                   filename: str = "", source: str = "csv", actor: str = "human") -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        queries = self.store.get_queries(case_id, it)
        if variant not in queries:
            raise OrchestratorError(f"反復 {it} の {variant} 案がありません（先に検索式を組んでください）")
        q = queries[variant]
        db = self._db()
        if docs is not None:
            from .db.adapter import RunResult
            result = RunResult(query_id=q.query_id, hit_count=hit_count if hit_count is not None else len(docs),
                               documents=docs, source=source, dialect=dialect or case["dialect"], csv_path=filename)
        else:
            payload = data if data is not None else (text or "")
            if not payload:
                raise OrchestratorError("CSV が空です")
            result = db.import_csv(payload, cfgmod.load_csv_dialect(case.get("csv_dialect") or "jplatpat"),
                                   query_id=q.query_id, hit_count=hit_count, dialect=dialect or case["dialect"], csv_path=filename)
        self._observe_codes(result.documents)
        run_id = self.store.add_run(case_id=case_id, query_id=q.query_id, iteration=it, variant=variant,
                                    dialect=result.dialect, source=result.source, hit_count=result.hit_count,
                                    docs=result.documents, csv_path=result.csv_path, info=result.info, actor=actor)
        ids = set(result.doc_ids())
        seeds = set(case["seeds"])
        recall_seed = (len(seeds & ids) / len(seeds)) if seeds else None
        self.store.add_decision(case_id, it, "G3", actor, "import_run",
                                {"variant": variant, "hit_count": result.hit_count, "n_docs": len(ids), "source": result.source, "file": filename})
        self._log(case_id, "G3", actor, "import_run", f"{variant} hits={result.hit_count} docs={len(ids)} source={result.source}")
        return {"run_id": run_id, "variant": variant, "hit_count": result.hit_count, "n_docs": len(ids),
                "recall_seed": recall_seed, "seeds_missing": sorted(seeds - ids), "info": result.info}

    def run_local(self, case_id: str, variants=None, actor: str = "policy") -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        queries = self.store.get_queries(case_id, it)
        if not queries:
            raise OrchestratorError("検索式がありません")
        db = self._db()
        out = {}
        for v in variants or VARIANTS:
            if v not in queries:
                continue
            try:
                res = db.run_local(queries[v], dialect=case["dialect"])
            except DBError as e:
                raise OrchestratorError(str(e)) from e
            out[v] = self.import_run(case_id, v, docs=res.documents, hit_count=res.hit_count, source="local_index", actor=actor)
        return out

    def _population(self, case: dict) -> tuple[dict | None, list[Document]]:
        """母集団 U: 広め案の実行結果（無ければ標準、狭め）。"""
        for v in VARIANTS:
            run = self.store.latest_run(case["case_id"], v, case["iteration"])
            if run and run.get("n_docs"):
                return run, self.store.run_documents(run["run_id"])
        # 前反復の母集団を流用（再反復で DB を叩く前）
        if case["iteration"] > 1:
            for v in VARIANTS:
                run = self.store.latest_run(case["case_id"], v)
                if run and run.get("n_docs"):
                    return run, self.store.run_documents(run["run_id"])
        return None, []

    # ================================================================ 採点（P4）
    def score(self, case_id: str, confirmed: bool = False, actor: str = "system", sample_seed: int | None = None,
              max_docs: int | None = None) -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        run, U = self._population(case)
        if not U:
            raise OrchestratorError("母集団がありません（G3 で CSV を取り込むか、local_index で実行してください）")
        axes = self.store.get_axes(case_id)
        terms_by_axis = self._axis_terms(case_id)
        axes_for_llm = [{"axis_id": a["axis_id"], "name": a["name"], "kind": a["kind"], "definition": a["definition"],
                         "terms": terms_by_axis.get(a["axis_id"], [])} for a in axes]
        eff = self.store.effective_judgments(case_id)
        K = int(self.cfg.get("top_k", 30))
        scfg = self.cfg.get("sample") or {}
        thr = int(self.cfg.get("relevance_threshold", 2))
        u_ids = [d.doc_id for d in U]
        by_id = {d.doc_id: d for d in U}
        targets: list[tuple[str, str]] = []
        for doc_id in u_ids[:K]:
            if doc_id not in eff:
                targets.append((doc_id, "topk"))
        # 無作為標本: s ≥ s_min になるまで m_step ずつ、m ≤ m_max
        sampled = self.store.sample_doc_ids(case_id)
        s_now = sum(1 for d in sampled if d in eff and eff[d]["overall"] >= thr)
        m_now = len(sampled)
        new_sample: list[str] = []
        if s_now < int(scfg.get("s_min", 30)) and m_now < int(scfg.get("m_max", 300)):
            seed = sample_seed if sample_seed is not None else int(hashlib.sha256(f"{case_id}:{it}:{m_now}".encode()).hexdigest()[:8], 16)
            m_step = min(int(scfg.get("m_step", 30)), int(scfg.get("m_max", 300)) - m_now)
            new_sample = draw_sample(u_ids, m_step, seed, exclude=sampled)
            if new_sample:
                self.store.save_sample(case_id=case_id, iteration=it, population_query_id=run["query_id"] if run else "",
                                       target_query_id="", seed=seed, doc_ids=new_sample)
            for doc_id in new_sample:
                if doc_id not in eff and doc_id not in {t[0] for t in targets}:
                    targets.append((doc_id, "sample"))
        if max_docs:
            targets = targets[:max_docs]
        llm = self._llm(case)
        n_judged = n_review = 0
        overall_rule = self._purpose(case).get("overall_rule", "min_required")
        for doc_id, selection in targets:
            d = by_id[doc_id]
            res = llm.complete("P4", {"axes": axes_for_llm, "overall_rule": overall_rule,
                                      "doc": {"doc_id": d.doc_id, "title": d.title, "abstract": d.abstract[:1500], "claims": d.claims[:1500]}},
                               confirmed=confirmed)
            m = res.majority
            self.store.add_judgment(case_id=case_id, doc_id=doc_id, iteration=it, selection=selection, judge="llm",
                                    overall=int(m.get("overall", 0)), per_axis=m.get("per_axis") or {}, flip_rate=res.flip_rate,
                                    needs_review=res.needs_review or res.low_confidence, rationale=str(m.get("rationale", "")))
            n_judged += 1
            n_review += int(res.needs_review or res.low_confidence)
        pool_size = self._rebuild_pool(case_id, it)
        if it == 1 and not (case.get("settings") or {}).get("first_top_relevant"):
            eff2 = self.store.effective_judgments(case_id)
            first_top = [d for d in u_ids[:K] if d in eff2 and eff2[d]["overall"] >= thr]
            self._save_settings(case_id, first_top_relevant=first_top)
        self._log(case_id, "G4", "llm", "score", f"judged={n_judged} needs_review={n_review} sample+={len(new_sample)} pool={pool_size}")
        return {"judged": n_judged, "needs_review": n_review, "sample_added": len(new_sample), "pool": pool_size,
                "population": {"run_id": run["run_id"] if run else None, "size": len(U), "variant": run["variant"] if run else None}}

    def _rebuild_pool(self, case_id: str, iteration: int) -> int:
        """プール = 既知文献 ∪ 有効判定が適合の文献（人の判定を優先）。"""
        case = self._case(case_id)
        thr = int(self.cfg.get("relevance_threshold", 2))
        eff = self.store.effective_judgments(case_id)
        want = set(case["seeds"]) | {d for d, j in eff.items() if (j["overall"] or 0) >= thr}
        have = self.store.pool_ids(case_id)
        for d in have - want:
            self.store.remove_from_pool(case_id, d)
        new = want - have
        if new:
            self.store.add_to_pool(case_id, sorted(new), "judgment", iteration)
        return len(want)

    # ================================================================ 分析（RSJ・変換・推定）→ G4
    def _judged_docs(self, case: dict, U: list[Document]) -> tuple[list[tuple[Document, bool]], dict, dict]:
        thr = int(self.cfg.get("relevance_threshold", 2))
        eff = self.store.effective_judgments(case["case_id"])
        relevant = {d: (j["overall"] or 0) >= thr for d, j in eff.items()}
        grades = {d: int(j["overall"] or 0) for d, j in eff.items()}
        u_ids = {d.doc_id for d in U}
        judged = [(d, relevant[d.doc_id]) for d in U if d.doc_id in relevant]
        for sid in case["seeds"]:                       # 既知文献は常に適合として含める
            if sid not in u_ids:
                doc = self.store.get_document(sid)
                if doc:
                    judged.append((doc, True))
        return judged, relevant, grades

    def _current_stats(self, case: dict) -> dict | None:
        run, U = self._population(case)
        if not U:
            return None
        judged, relevant, grades = self._judged_docs(case, U)
        if len(judged) < 4:
            return None
        rcfg = self.cfg.get("rsj") or {}
        std = self.store.get_queries(case["case_id"], case["iteration"]).get("standard") or self.store.get_queries(case["case_id"]).get("standard")
        q_terms = {t.text for b in std.active_blocks() for t in b.adopted_terms()} if std else set()
        q_codes = {(c.scheme, c.code) for b in std.active_blocks() for c in b.adopted_codes()} if std else set()
        stats = rank_candidates(judged, stopwords=self.stopwords, sample_ids=self.store.sample_doc_ids(case["case_id"]),
                                grades=grades, top_terms=int(rcfg.get("top_terms", 30)), top_codes=int(rcfg.get("top_codes", 20)),
                                min_tf=int(rcfg.get("min_tf", 2)), code_level_ratio=float(rcfg.get("code_level_n_ratio", 0.5)),
                                query_terms=q_terms, query_codes=q_codes)
        for key in ("codes", "negative_codes"):
            for s in stats[key]:
                s["title"] = self.dictionary.title(s["scheme"], s["feature"])
        return stats

    def analyze(self, case_id: str, confirmed: bool = False, actor: str = "system") -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        run, U = self._population(case)
        if not U:
            raise OrchestratorError("母集団がありません（G3 で実行結果を取り込んでください）")
        queries = self.store.get_queries(case_id, it)
        if not queries:
            raise OrchestratorError("検索式がありません")
        axes = self.store.get_axes(case_id)
        judged, relevant, grades = self._judged_docs(case, U)
        pool_ids = self.store.pool_ids(case_id)
        seeds = set(case["seeds"])
        K = int(self.cfg.get("top_k", 30))
        tau = float(self.cfg.get("tau_pool", 0.95))
        purpose = self._purpose(case)
        u_ids = [d.doc_id for d in U]

        # --- 指標（案ごと）
        variants_m: dict[str, dict] = {}
        hits_by_variant: dict[str, set[str]] = {}
        for v, q in queries.items():
            r = self.store.latest_run(case_id, v, it)
            if r and r.get("n_docs"):
                ids = self.store.run_doc_ids(r["run_id"])
                hit_ids, hit_count, source = set(ids), r["hit_count"], r["source"]
                ranked = ids
                meta = {"executed_at": r["executed_at"], "csv_path": r.get("csv_path", ""), "run_id": r["run_id"]}
            else:
                hit_ids = match_ids(q, U)
                hit_count, source, ranked = len(hit_ids), "local(U)", [d for d in u_ids if d in hit_ids]
                meta = {"executed_at": "", "csv_path": "", "run_id": None}
            m = evaluate(hit_ids, hit_count, ranked, seeds, pool_ids, relevant, k=K, complexity=q.complexity())
            md = m.to_dict()
            md.update(meta)
            md.update({"variant": v, "source": source, "query_id": q.query_id,
                       "in_range": in_range(hit_count, purpose.get("population_range")),
                       "constraints_ok": satisfies_constraints(m, tau)})
            variants_m[v] = md
            hits_by_variant[v] = hit_ids

        # --- RSJ
        stats = self._current_stats(case) or {"N": len(judged), "R": sum(1 for _, r in judged if r), "terms": [], "codes": [],
                                              "negative_terms": [], "negative_codes": [], "note": "採点済み文献が少なく統計を出せません"}
        # --- 変換候補（RSJ・決定木・P5）
        base = queries.get("standard") or next(iter(queries.values()))
        cands = self.store.get_candidates(case_id)
        aux_axes = []
        present = {b.axis_id for b in base.active_blocks()}
        for a in axes:
            if a["axis_id"] in present:
                continue
            terms = [{"text": c["value"], "fields": ["AB", "CL"], "origin": c["origin"]} for c in cands
                     if c["kind"] == "term" and c["axis_id"] == a["axis_id"] and c["status"] == "adopted"]
            codes = [{"scheme": c["scheme"], "code": c["value"], "origin": c["origin"], "title": c.get("title", "")} for c in cands
                     if c["kind"] == "code" and c["axis_id"] == a["axis_id"] and c["status"] == "adopted"]
            aux_axes.append({"axis_id": a["axis_id"], "name": a["name"], "terms": terms, "codes": codes})
        tcfg = self.cfg.get("tree") or {}
        all_axis_terms = self._axis_terms(case_id)
        transforms = propose_from_stats(base, stats, aux_axes, it, code_ratio=float((self.cfg.get("rsj") or {}).get("code_level_n_ratio", 0.5)),
                                        axis_terms=all_axis_terms)
        tree_ts, dnf = tree_transforms(judged, base, stopwords=self.stopwords, max_depth=int(tcfg.get("max_depth", 4)),
                                       min_samples_leaf=int(tcfg.get("min_samples_leaf", 3)),
                                       min_tf=int((self.cfg.get("rsj") or {}).get("min_tf", 2)),
                                       exclusions_enabled=bool(self.cfg.get("exclusions_enabled")), axis_terms=all_axis_terms)
        transforms += tree_ts
        p5_info = {}
        try:
            r5 = self._llm(case).complete("P5", {"dsl_summary": summarize_query(base),
                                                 "axes": [{"axis_id": a["axis_id"], "name": a["name"], "kind": a["kind"], "definition": a["definition"]} for a in axes],
                                                 "stats": {k: stats[k][:10] for k in ("terms", "codes", "negative_terms", "negative_codes")},
                                                 "ops": ALL_OPS}, confirmed=confirmed)
            p5_info = {"mode": r5.mode, "n": len(r5.majority.get("transforms", []))}
            for t in r5.majority.get("transforms", []):
                if t.get("op") not in ALL_OPS:
                    continue
                tr = Transform(t["op"], dict(t.get("target") or {}), source="llm", reason=str(t.get("reason", "")))
                tr.direction = direction_of(tr.op, tr.target, base)
                transforms.append(tr)
        except LLMError as e:
            p5_info = {"error": str(e)}
        transforms = dedupe_transforms(transforms)
        protected = {norm_text(t.text) for b in base.active_blocks() for t in b.adopted_terms() if t.origin in ("input", "human")}
        for t in transforms:
            local_evaluate(t, base, U, pool_ids, relevant, k=K, iteration=it,
                           exclusions_enabled=bool(self.cfg.get("exclusions_enabled")))
            if t.op == "DROP_TERM" and norm_text(str(t.target.get("text", ""))) in protected and t.status != "invalid":
                t.status = "rejected"                # ガード: 人が入れた語（input／human 由来）は自動提案で削除しない
                t.reason = (t.reason + " / " if t.reason else "") + "入力由来の語は削除候補にしない（ガード）"
        # LLM 提案と統計候補の一致率（ドリフト監視）
        stat_keys = {t.op + json.dumps(t.target, sort_keys=True, ensure_ascii=False) for t in transforms if t.source in ("rsj", "tree")}
        llm_ts = [t for t in transforms if t.source == "llm"]
        llm_agree = (sum(1 for t in llm_ts if t.op + json.dumps(t.target, sort_keys=True, ensure_ascii=False) in stat_keys) / len(llm_ts)) if llm_ts else None
        self.store.save_transforms(case_id, it, [t.to_dict() for t in transforms if t.status != "invalid"])

        # --- 無作為標本による推定
        scfg = self.cfg.get("sample") or {}
        sample_ids = self.store.sample_doc_ids(case_id)
        sample_j = {d: relevant[d] for d in sample_ids if d in relevant}
        samples = self.store.get_samples(case_id)
        seed = samples[-1]["seed"] if samples else None
        estimates = {}
        for v in ("standard", "narrow", "broad"):
            if v in hits_by_variant and sample_j:
                est = estimate_recall(sample_j, hits_by_variant[v], len(U), s_min=int(scfg.get("s_min", 30)),
                                      z=float(scfg.get("z", 1.96)), seed=seed)
                estimates[v] = est.to_dict()
        # --- CAL
        cal = {}
        ccfg = self.cfg.get("cal") or {}
        labels = {d.doc_id: rel for d, rel in judged if d.doc_id in set(u_ids)}
        if len(labels) >= 10 and len(U) >= 20 and any(labels.values()):
            try:
                cal = cal_round(U, labels, ccfg, seed=it, k=int(ccfg.get("batch_human", 20)),
                                provisional_negatives=int(ccfg.get("provisional_negatives", 100)))
                cal = {k: v for k, v in cal.items() if k != "scores"} | {"top_scores": sorted(cal["scores"].items(), key=lambda x: -x[1])[:30]}
            except ValueError as e:
                cal = {"ok": False, "reason": str(e)}
        # --- ドリフト（初回上位適合文献の保持率）
        first_top = (case.get("settings") or {}).get("first_top_relevant") or []
        retention = None
        if first_top and "standard" in hits_by_variant:
            retention = len(set(first_top) & hits_by_variant["standard"]) / len(first_top)
        # --- パレート・推奨
        feasible = [m for m in variants_m.values() if m["constraints_ok"]]
        front = pareto_front(feasible) if feasible else []
        recommended = select_by_policy(front, purpose.get("selection", "recall_first"), purpose.get("population_range"))
        # --- 収束カウント
        settings = case.get("settings") or {}
        prev_iters = self.store.get_iterations(case_id)
        prev_std = (prev_iters[-1]["metrics"].get("variants", {}).get("standard") if prev_iters and prev_iters[-1]["iteration"] < it else None)
        no_improve = int(settings.get("no_improve_count") or 0)
        cur_std = variants_m.get("standard")
        if prev_std and cur_std:
            from .eval.metrics import is_pareto_improvement
            no_improve = 0 if is_pareto_improvement(cur_std, prev_std) else no_improve + 1
        stop = self._stop_status(case, variants_m, estimates, no_improve)
        judged_summary = {"N": len(judged), "R": sum(1 for _, r in judged if r), "pool": len(pool_ids),
                          "needs_review": sum(1 for j in self.store.get_judgments(case_id) if j["needs_review"] and j["judge"] == "llm" and j["iteration"] == it),
                          "sample_m": len(sample_ids), "sample_s": sum(1 for v in sample_j.values() if v)}
        metrics = {"iteration": it, "population": {"run_id": run["run_id"] if run else None, "size": len(U), "variant": run["variant"] if run else None,
                                                   "source": run["source"] if run else None},
                   "variants": variants_m, "estimates": estimates, "judged": judged_summary,
                   "stats": {"N": stats["N"], "R": stats["R"], "terms": stats["terms"][:15], "codes": stats["codes"][:10],
                             "negative_terms": stats["negative_terms"][:10], "negative_codes": stats["negative_codes"][:5], "note": stats.get("note", "")},
                   "dnf": dnf, "cal": cal, "retention": retention,
                   "retention_warning": retention is not None and retention < float(self.cfg.get("retention_warn", 0.9)),
                   "llm_agreement_with_stats": None if llm_agree is None else round(llm_agree, 3), "p5": p5_info,
                   "pareto": {"front": [m["variant"] for m in front], "recommended": recommended["variant"] if recommended else None},
                   "stop": stop, "no_improve_count": no_improve, "created_at": now_iso()}
        self.store.save_iteration_metrics(case_id, it, metrics)
        self._save_settings(case_id, no_improve_count=no_improve)
        self.store.update_case(case_id, status="g4_pending")
        self._log(case_id, "G4", actor, "analyze", f"transforms={len(transforms)} stop={stop['should_stop']} recommended={metrics['pareto']['recommended']}")
        return {"metrics": metrics, "transforms": self.store.get_transforms(case_id, it)}

    def _stop_status(self, case: dict, variants_m: dict, estimates: dict, no_improve: int) -> dict:
        std = variants_m.get("standard") or {}
        budget = self.cfg.get("budget") or {}
        purpose = self._purpose(case)
        seed_ok = std.get("recall_seed") in (None, 1.0) if std else False
        pool_ok = (std.get("recall_pool") is None) or (std.get("recall_pool") >= float(self.cfg.get("tau_pool", 0.95)))
        size_ok = bool(std.get("in_range"))
        est = estimates.get("standard") or {}
        est_ok = bool(est) and not est.get("low_confidence") and (est.get("ci_low") or 0) >= float(self.cfg.get("rho_target", 0.9))
        converged = no_improve >= int(self.cfg.get("converge_T", 3))
        it = case["iteration"]
        n_runs = len(self.store.get_runs(case["case_id"]))
        budget_out = (it >= int(budget.get("iterations", 10)) or self.store.count_llm_calls(case["case_id"]) >= int(budget.get("llm_calls", 300))
                      or n_runs >= int(budget.get("db_runs", 30)))
        required_met = bool(std) and seed_ok and pool_ok
        should_stop = required_met and (size_ok or est_ok or converged or budget_out)
        reasons = []
        rng = purpose.get("population_range") or [None, None]
        if not std:
            reasons.append("標準案の指標なし")
        if not seed_ok:
            reasons.append("既知文献再現率 < 1.0（必須条件 未達）")
        if not pool_ok:
            reasons.append("プール再現率 < τ（必須条件 未達）")
        if size_ok:
            reasons.append("母集団サイズがレンジ内")
        else:
            reasons.append(f"母集団サイズ {std.get('hit_count')} がレンジ外（目標 {rng[0]}〜{rng[1]}）")
        if est_ok:
            reasons.append("推定再現率の下側信頼限界 ≥ ρ")
        elif est:
            reasons.append(f"推定再現率 CI 下限 {est.get('ci_low')} < ρ" + ("（標本不足で低信頼）" if est.get("low_confidence") else ""))
        if converged:
            reasons.append(f"パレート改善なし {no_improve} 反復（収束）")
        if budget_out:
            reasons.append("予算上限")
        return {"seed_ok": seed_ok, "pool_ok": pool_ok, "size_ok": size_ok, "estimate_ok": est_ok, "converged": converged,
                "budget_exhausted": budget_out, "required_met": required_met, "should_stop": should_stop, "reasons": reasons,
                "population_range": purpose.get("population_range")}

    # ================================================================ G4
    def decide_g4(self, case_id: str, judgments: list[dict] | None = None, transforms: list[dict] | None = None,
                  action: str = "finalize", actor: str = "human", confirmed: bool = False) -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        if action not in ("finalize", "iterate"):
            raise OrchestratorError("action は finalize か iterate")
        for j in judgments or []:
            doc_id = str(j.get("doc_id") or "").strip()
            if not doc_id or j.get("overall") is None:
                continue
            self.store.add_judgment(case_id=case_id, doc_id=doc_id, iteration=it, selection="review", judge="human",
                                    overall=max(0, min(3, int(j["overall"]))), per_axis=j.get("per_axis") or {},
                                    rationale=str(j.get("comment") or ""))
        self._rebuild_pool(case_id, it)
        tf_rows = {t["transform_id"]: t for t in self.store.get_transforms(case_id, it)}
        n_adopted = 0
        for d in transforms or []:
            row = tf_rows.get(d.get("transform_id"))
            status = d.get("status")
            if not row or status not in ("adopted", "rejected"):
                continue
            if status == "adopted" and row.get("regression"):
                status = "rejected"      # 退行検知した変換は採用できない（ガード）
            self.store.decide_transform(row["transform_id"], status, actor)
            if status == "adopted":
                self._apply_transform_to_case(case_id, row, it, actor)
                n_adopted += 1
        self.store.add_decision(case_id, it, "G4", actor, action,
                                {"n_judgments": len(judgments or []), "n_transforms_adopted": n_adopted})
        self._log(case_id, "G4", actor, action, f"judgments={len(judgments or [])} transforms_adopted={n_adopted}")
        if action == "finalize":
            return self._finalize(case_id, actor, confirmed)
        # 再反復: 版を進め、候補を再展開（RSJ 統計付き）
        self.store.update_case(case_id, iteration=it + 1, status="axes_fixed")
        self.expand(case_id, confirmed=confirmed, actor=actor)
        return {"status": "g2_pending", "iteration": it + 1}

    def _apply_transform_to_case(self, case_id: str, t: dict, it: int, actor: str) -> None:
        op, tg = t["op"], t.get("target") or {}
        origin = f"{t.get('source', 'human')}:iter{it}"
        axes = {a["axis_id"]: a for a in self.store.get_axes(case_id)}
        case = self._case(case_id)
        settings = dict(case.get("settings") or {})
        if op == "ADD_TERM":
            existing = self.store.find_candidate(case_id, "term", tg.get("axis_id", ""), tg.get("text", ""))
            if existing:
                self.store.decide_candidate(existing["candidate_id"], "adopted", "01", None, actor)
            else:
                self._add_term_candidate(case_id, tg.get("axis_id"), tg.get("text"), "extracted", origin, "", None, it,
                                         status="adopted", decided_by=actor, decided_at=now_iso(), reason_code="01")
        elif op == "DROP_TERM":
            for c in self.store.get_candidates(case_id, kind="term"):
                if c["axis_id"] == tg.get("axis_id") and norm_text(c["value"]) == norm_text(tg.get("text", "")):
                    self.store.decide_candidate(c["candidate_id"], "rejected", "02", None, actor)
        elif op == "ADD_CODE":
            code = codelib.normalize(tg["scheme"], tg["code"])
            existing = self.store.find_candidate(case_id, "code", tg.get("axis_id", ""), code, tg["scheme"])
            if existing:
                self.store.decide_candidate(existing["candidate_id"], "adopted", "01", None, actor)
            else:
                self._add_code_candidate(case_id, tg.get("axis_id"), tg["scheme"], code, origin, "", None, it,
                                         status="adopted", decided_by=actor, decided_at=now_iso(), reason_code="01")
        elif op == "DROP_CODE":
            code = codelib.normalize(tg["scheme"], tg["code"])
            for c in self.store.get_candidates(case_id, kind="code"):
                if c["scheme"] == tg["scheme"] and c["value"] == code:
                    self.store.decide_candidate(c["candidate_id"], "rejected", "02", None, actor)
        elif op in ("CODE_LEVEL_UP", "CODE_LEVEL_DOWN"):
            code = codelib.normalize(tg["scheme"], tg["code"])
            new_code = codelib.normalize(tg["scheme"], tg.get("new_code") or codelib.coarser(tg["scheme"], code) or "")
            for c in self.store.get_candidates(case_id, kind="code"):
                if c["scheme"] == tg["scheme"] and c["value"] == code and c["status"] == "adopted":
                    self.store.decide_candidate(c["candidate_id"], "rejected", "06" if op == "CODE_LEVEL_UP" else "03", None, actor)
            if new_code:
                existing = self.store.find_candidate(case_id, "code", tg.get("axis_id", ""), new_code, tg["scheme"])
                if existing:
                    self.store.decide_candidate(existing["candidate_id"], "adopted", "01", None, actor)
                else:
                    self._add_code_candidate(case_id, tg.get("axis_id"), tg["scheme"], new_code, origin, "", None, it,
                                             status="adopted", decided_by=actor, decided_at=now_iso(), reason_code="01")
        elif op == "ADD_AXIS":
            lst = list(settings.get("extra_and_axes") or [])
            if tg.get("axis_id") in axes and tg["axis_id"] not in lst:
                lst.append(tg["axis_id"])
            settings["extra_and_axes"] = lst
            settings["dropped_axes"] = [a for a in settings.get("dropped_axes") or [] if a != tg.get("axis_id")]
        elif op == "DROP_AXIS":
            lst = list(settings.get("dropped_axes") or [])
            if tg.get("axis_id") in axes and tg["axis_id"] not in lst:
                lst.append(tg["axis_id"])
            settings["dropped_axes"] = lst
            settings["extra_and_axes"] = [a for a in settings.get("extra_and_axes") or [] if a != tg.get("axis_id")]
        elif op == "TERM_CODE_JOIN":
            lst = set(settings.get("join_and_axes") or [])
            if (tg.get("join") or "AND") == "AND":
                lst.add(tg["axis_id"])
            else:
                lst.discard(tg["axis_id"])
            settings["join_and_axes"] = sorted(lst)
        elif op == "FIELD_CHANGE":
            ov = dict(settings.get("fields_override") or {})
            ov[tg["axis_id"]] = list(tg.get("fields") or ["AB", "CL"])
            settings["fields_override"] = ov
        elif op in ("PROXIMITY_ON", "PROXIMITY_OFF"):
            px = dict(settings.get("proximity") or {})
            if op == "PROXIMITY_ON":
                px[tg["axis_id"]] = int(tg.get("distance") or 5)
            else:
                px.pop(tg["axis_id"], None)
            settings["proximity"] = px
        elif op == "ADD_EXCLUSION" and self.cfg.get("exclusions_enabled"):
            ex = list(settings.get("exclusions") or [])
            ex.append({"terms": tg.get("terms") or [], "codes": tg.get("codes") or []})
            settings["exclusions"] = ex
        elif op == "SPLIT_BLOCK":
            new_axis = tg.get("new_axis_id") or f"{tg['axis_id']}2"
            axes_list = self.store.get_axes(case_id)
            if new_axis not in axes:
                src = axes.get(tg["axis_id"], {})
                axes_list.append({"axis_id": new_axis, "name": tg.get("name") or f"{src.get('name', '')}（分割）", "kind": "required",
                                  "definition": src.get("definition", ""), "origin": f"split:{actor}", "evidence": "", "fixed": True})
                self.store.replace_axes(case_id, axes_list, actor)
            move = {norm_text(x) for x in tg.get("terms") or []}
            for c in self.store.get_candidates(case_id, kind="term"):
                if c["axis_id"] == tg["axis_id"] and norm_text(c["value"]) in move:
                    self.store.decide_candidate(c["candidate_id"], c["status"], c.get("reason_code", ""), None, actor, axis_id=new_axis)
        elif op == "MERGE_BLOCK":
            for c in self.store.get_candidates(case_id):
                if c["axis_id"] == tg.get("other_axis_id"):
                    self.store.decide_candidate(c["candidate_id"], c["status"], c.get("reason_code", ""), None, actor, axis_id=tg["axis_id"])
            axes_list = [a for a in self.store.get_axes(case_id) if a["axis_id"] != tg.get("other_axis_id")]
            self.store.replace_axes(case_id, axes_list, actor)
        self.store.update_case(case_id, settings=settings)

    def _finalize(self, case_id: str, actor: str, confirmed: bool) -> dict:
        case = self._case(case_id)
        it = case["iteration"]
        # 辞書の育成（採否の最終状態を反映）
        axes = {a["axis_id"]: a for a in self.store.get_axes(case_id)}
        for c in self.store.get_candidates(case_id):
            if c["status"] not in ("adopted", "rejected") or not c.get("decided_by"):
                continue
            if c["kind"] == "code" and c["axis_id"] in axes:
                self.store.bump_axis_code_dictionary(axes[c["axis_id"]]["name"], c["scheme"], c["value"], c["status"] == "adopted")
        # P6 根拠文（数値は Python 側の値をそのまま渡す）
        explanations = []
        try:
            cands = [c for c in self.store.get_candidates(case_id) if c["status"] == "adopted"]
            r6 = self._llm(case).complete("P6", {"axes": [{"axis_id": a["axis_id"], "name": a["name"], "kind": a["kind"]} for a in axes.values()],
                                                 "candidates": [{k: c.get(k) for k in ("kind", "axis_id", "value", "scheme", "title", "origin", "r", "n", "R", "N", "rsj_w", "flip_rate", "reason_code")} for c in cands]},
                                          confirmed=confirmed)
            explanations = r6.majority.get("explanations", [])
        except LLMError as e:
            explanations = [{"kind": "summary", "value": "", "text": f"（P6 未実行: {e}）"}]
        self._save_settings(case_id, explanations=explanations, finalized_at=now_iso(), final_iteration=it)
        self.store.update_case(case_id, status="finalized")
        self._log(case_id, "G4", actor, "finalize", f"iteration={it}")
        return {"status": "finalized", "iteration": it}

    # ================================================================ 自動運転（Decider 差し替え）
    def run_case(self, case_id: str, decider, max_iterations: int | None = None, confirmed: bool = True) -> dict:
        budget = self.cfg.get("budget") or {}
        max_it = max_iterations or int(budget.get("iterations", 10))
        steps = 0
        while steps < 200:
            steps += 1
            case = self._case(case_id)
            status = case["status"]
            it = case["iteration"]
            if status == "finalized":
                break
            if status == "new":
                self.structure(case_id, confirmed=confirmed)
                continue
            if status == "g1_pending":
                d = decider.decide("G1", {"axes": self.store.get_axes(case_id), "case": case})
                self.decide_g1(case_id, d.payload.get("axes") or self.store.get_axes(case_id), actor=d.actor)
                continue
            if status == "axes_fixed":
                self.expand(case_id, confirmed=confirmed)
                continue
            if status == "g2_pending":
                d = decider.decide("G2", {"candidates": self.store.get_candidates(case_id), "case": case, "iteration": it})
                self.decide_g2(case_id, d.payload.get("decisions") or [], d.payload.get("extra"), d.payload.get("broad_drop_axis"), actor=d.actor)
                continue
            if status == "g3_pending":
                runs = self.store.get_runs(case_id, it)
                if not runs:
                    d = decider.decide("G3", {"queries": {v: q.to_dict() for v, q in self.store.get_queries(case_id, it).items()}, "case": case})
                    mode = d.payload.get("mode") or "local_index"
                    if mode == "csv" and not d.payload.get("runs") and self.store.local_index_size():
                        mode = "local_index"
                    if mode == "local_index":
                        self.run_local(case_id, actor=d.actor)
                    elif mode == "api":
                        db = self._db()
                        for v, q in self.store.get_queries(case_id, it).items():
                            rend = self.store.get_renderings(q.query_id)
                            text = next((r["text"] for r in rend if r["dialect"] == case["dialect"]), "")
                            res = db.run_api(q, text, case["dialect"])
                            self.import_run(case_id, v, docs=res.documents, hit_count=res.hit_count, source="api", actor=d.actor)
                    else:
                        for r in d.payload.get("runs") or []:
                            self.import_run(case_id, r["variant"], data=Path(r["csv_path"]).read_bytes(), hit_count=r.get("hit_count"),
                                            filename=r["csv_path"], actor=d.actor)
                        if not self.store.get_runs(case_id, it):
                            raise PendingHuman("G3", {"message": "DB で実行して CSV を取り込んでください"})
                self.score(case_id, confirmed=confirmed)
                self.analyze(case_id, confirmed=confirmed)
                continue
            if status == "g4_pending":
                metrics = self.store.get_iterations(case_id)[-1]["metrics"]
                ctx = {"transforms": self.store.get_transforms(case_id, it), "metrics": metrics, "stop": metrics.get("stop"),
                       "base_metrics": (metrics.get("variants") or {}).get("standard"), "case": case, "iteration": it}
                d = decider.decide("G4", ctx)
                action = d.payload.get("action") or d.action
                if action == "iterate" and it >= max_it:
                    action = "finalize"
                self.decide_g4(case_id, d.payload.get("judgments"), d.payload.get("transforms"), action, actor=d.actor, confirmed=confirmed)
                continue
            raise OrchestratorError(f"不明な状態: {status}")
        return self.summary(case_id)

    def run_auto(self, case_id: str, max_iterations: int | None = None) -> dict:
        from .gates.policy import PolicyDecider
        case = self._case(case_id)
        return self.run_case(case_id, PolicyDecider(self.cfg, self._purpose(case)), max_iterations=max_iterations)

    # ================================================================ Excel 設計シート
    def apply_excel(self, case_id: str, data: bytes, actor: str = "human") -> dict:
        """編集済みの設計シートから採否・判定・変換の判断を取り込む（ゲートの確定は別途）。"""
        from .ui.excel import parse_design_workbook
        case = self._case(case_id)
        parsed = parse_design_workbook(data)
        cands = {c["candidate_id"]: c for c in self.store.get_candidates(case_id)}
        n_dec = 0
        for d in parsed["decisions"]:
            c = cands.get(d["candidate_id"])
            if c and c["status"] != d["status"]:
                self.store.decide_candidate(c["candidate_id"], d["status"], d.get("reason_code", ""), d.get("note"), actor)
                n_dec += 1
        it = case["iteration"]
        for j in parsed["judgments"]:
            self.store.add_judgment(case_id=case_id, doc_id=j["doc_id"], iteration=it, selection="review", judge="human",
                                    overall=j["overall"], per_axis=j.get("per_axis") or {}, rationale=j.get("comment", ""))
        if parsed["judgments"]:
            self._rebuild_pool(case_id, it)
        tfs = {t["transform_id"]: t for t in self.store.get_transforms(case_id)}
        n_tf = 0
        for t in parsed["transforms"]:
            row = tfs.get(t["transform_id"])
            if row and row["status"] == "candidate":
                self.store.decide_transform(row["transform_id"], t["status"], actor)
                n_tf += 1
        self._log(case_id, "-", actor, "excel_import", f"decisions={n_dec} judgments={len(parsed['judgments'])} transforms={n_tf}")
        return {"decisions": n_dec, "judgments": len(parsed["judgments"]), "transforms": n_tf}

    # ================================================================ 参照
    def answer_manual(self, case_id: str, call_key: str, text: str, apply_all: bool = False) -> dict:
        n = 0
        if apply_all:
            row = self.store.get_manual_prompt(call_key)
            if row:
                for p in self.store.pending_manual_prompts(case_id):
                    if p["prompt_id"] == row["prompt_id"] and p["call_key"].rsplit(":", 1)[0] == call_key.rsplit(":", 1)[0]:
                        self.store.answer_manual_prompt(p["call_key"], text)
                        n += 1
        else:
            n = int(self.store.answer_manual_prompt(call_key, text))
        return {"answered": n, "pending": len(self.store.pending_manual_prompts(case_id))}

    def summary(self, case_id: str) -> dict:
        case = self._case(case_id)
        its = self.store.get_iterations(case_id)
        last = its[-1]["metrics"] if its else {}
        return {"case_id": case_id, "status": case["status"], "iteration": case["iteration"],
                "variants": last.get("variants", {}), "stop": last.get("stop", {}), "pareto": last.get("pareto", {}),
                "pool": len(self.store.pool_ids(case_id)), "llm_calls": self.store.count_llm_calls(case_id)}

    def judgment_table(self, case_id: str) -> list[dict]:
        rows: dict[str, dict] = {}
        docs_cache: dict[str, Document] = {}
        judgments = self.store.get_judgments(case_id)
        ids = {j["doc_id"] for j in judgments}
        docs_cache = self.store.get_documents(ids)
        for j in judgments:
            r = rows.setdefault(j["doc_id"], {"doc_id": j["doc_id"], "title": docs_cache.get(j["doc_id"], Document(j["doc_id"])).title,
                                              "iteration": j["iteration"], "selection": j["selection"], "llm_overall": None, "llm_per_axis": "",
                                              "flip_rate": None, "needs_review": False, "human_overall": None, "human_per_axis": "", "comment": "",
                                              "rationale": "", "seed": False})
            per_axis = ",".join(f"{k}:{v}" for k, v in sorted((j.get("per_axis") or {}).items()))
            if j["judge"] == "llm":
                r.update({"llm_overall": j["overall"], "llm_per_axis": per_axis, "flip_rate": j.get("flip_rate"),
                          "needs_review": j["needs_review"], "rationale": j.get("rationale", ""), "iteration": j["iteration"], "selection": j["selection"]})
            elif j["judge"] == "human":
                r.update({"human_overall": j["overall"], "human_per_axis": per_axis, "comment": j.get("rationale", "")})
            elif j["judge"] == "seed":
                r["seed"] = True
                r["selection"] = "seed"
            elif j["judge"] == "classifier":
                r["classifier"] = j["overall"]
        thr = int(self.cfg.get("relevance_threshold", 2))
        eff = self.store.effective_judgments(case_id)
        for doc_id, r in rows.items():
            e = eff.get(doc_id)
            r["effective_overall"] = e["overall"] if e else None
            r["relevant"] = bool(e and (e["overall"] or 0) >= thr)
        return sorted(rows.values(), key=lambda r: (not r["seed"], not r["needs_review"], -(r["effective_overall"] or 0), r["doc_id"]))

    def case_bundle(self, case_id: str) -> dict:
        case = self._case(case_id)
        queries = []
        for row in self.store.query_history(case_id):
            q = self.store.get_query(row["query_id"])
            queries.append({"query_id": row["query_id"], "iteration": row["iteration"], "variant": row["variant"],
                            "parent_query_id": row["parent_query_id"], "created_at": row["created_at"],
                            "dsl": q.to_dict() if q else None, "summary": summarize_query(q) if q else None,
                            "renderings": self.store.get_renderings(row["query_id"])})
        its = self.store.get_iterations(case_id)
        pending = self.store.pending_manual_prompts(case_id)
        return {
            "case": case, "axes": self.store.get_axes(case_id), "candidates": self.store.get_candidates(case_id),
            "queries": queries, "runs": self.store.get_runs(case_id), "iterations": its,
            "judgment_table": self.judgment_table(case_id), "pool": sorted(self.store.pool_ids(case_id)),
            "transforms": self.store.get_transforms(case_id), "samples": [{k: v for k, v in s.items() if k != "doc_ids"} | {"n_docs": len(s["doc_ids"])} for s in self.store.get_samples(case_id)],
            "decisions": self.store.get_decisions(case_id), "logs": self.store.get_logs(case_id),
            "llm_calls": self.store.get_llm_calls(case_id, 100), "pending_prompts": pending,
            "dictionary": self.store.dictionary_summary() | {"degraded": self.dictionary.degraded},
            "purpose": self._purpose(case), "local_index_size": self.store.local_index_size(),
            "config": {k: self.cfg.get(k) for k in ("top_k", "relevance_threshold", "tau_pool", "rho_target", "flip_threshold",
                                                    "converge_T", "budget", "sample", "exclusions_enabled", "offline", "production",
                                                    "reason_codes", "dialects", "default_dialect")}
                      | {"llm_mode": (self.cfg.get("llm") or {}).get("mode"), "db_mode": (self.cfg.get("db") or {}).get("mode")},
            "latest_metrics": its[-1]["metrics"] if its else None,
        }
