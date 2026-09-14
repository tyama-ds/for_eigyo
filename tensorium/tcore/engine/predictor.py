"""保存済み run から予測器を復元し、新しい表に予測を付ける。"""
from __future__ import annotations

from ..config import load_settings
from ..dataio import is_missing
from ..env import resolve_device
from ..prep import PrepError, Preproc, make_examples
from ..runs import load_meta, read_json, run_dir

PRED_BATCH = 64


class Predictor:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.meta = load_meta(run_id)
        if self.meta is None:
            raise PrepError("run が見つかりません")
        d = run_dir(run_id)
        self.dir = d
        self.preproc = Preproc.from_dict(read_json(d / "preproc.json"))
        self.spec = self.preproc.spec
        self.arch = read_json(d / "arch.json")
        self.family = self.arch["family"]
        self.task = self.arch["task"]
        self.classes = self.meta.get("classes")
        self._impl = None
        if self.family == "baseline":
            from .baseline import BaselinePredictor
            self._impl = BaselinePredictor(run_id)
        else:
            self._load_torch()

    # ---- torch 系
    def _load_torch(self):
        import torch

        from .common import FusionModel, TabularFeatures, parse_hidden
        from .pipeline import feature_tensors
        from .sbert import SentenceEmbedder
        from .tabular import FTTransformer
        from .text import (
            HFCollate,
            HFEncoder,
            ScratchCollate,
            ScratchEncoder,
            ScratchTokenizer,
            load_hf_model,
            load_hf_tokenizer,
        )

        cfg = load_settings()
        self.device = resolve_device(cfg.get("device", "auto"))
        hp = self.arch["hparams"]
        n_out = self.arch["n_out"]
        spec = self.spec
        cards = self.preproc.cat.cardinalities if spec["cat_cols"] else []
        num_dim = len(spec["num_cols"])
        tab = TabularFeatures(num_dim, cards) if (num_dim or cards) else None
        self._feature_tensors = feature_tensors
        self.text_collate = None
        self.embedder = None
        trust = bool(cfg.get("trust_remote_code"))
        if self.family == "hf":
            enc_dir = str(self.dir / "encoder")
            tok = load_hf_tokenizer(enc_dir, trust)
            enc = HFEncoder(load_hf_model(enc_dir, trust), self.arch["pooling"], freeze=True)
            self.model = FusionModel(enc, enc.out_dim, tab, parse_hidden(hp["head_hidden"]), n_out, 0.0)
            state = torch.load(self.dir / "head.pt", map_location="cpu")
            self.model.load_state_dict(state, strict=False)
            self.text_collate = HFCollate(tok, hp["max_len"], self.device)
        elif self.family == "sbert":
            self.embedder = SentenceEmbedder(self.arch["model_path"], hp["max_len"], self.device,
                                             hp["normalize"], hp["embed_batch"], trust)
            self.model = FusionModel(None, self.arch["enc_dim"], tab, parse_hidden(hp["head_hidden"]), n_out, 0.0)
            self.model.load_state_dict(torch.load(self.dir / "model.pt", map_location="cpu"))
        elif self.family == "scratch":
            tokenizer = ScratchTokenizer.from_dict(self.arch["tokenizer"])
            enc = ScratchEncoder(tokenizer.vocab_size, hp["d_model"], hp["nhead"], hp["layers"], hp["ff_dim"],
                                 hp["dropout"], hp["max_len"], hp["pooling"])
            self.model = FusionModel(enc, enc.out_dim, tab, [], n_out, 0.0)
            self.model.load_state_dict(torch.load(self.dir / "model.pt", map_location="cpu"))
            self.text_collate = ScratchCollate(tokenizer, hp["max_len"], self.device)
        elif self.family == "tabular":
            enc = FTTransformer(self.arch["num_dim"], self.arch["cat_cards"], hp["d_token"], hp["nhead"],
                                hp["layers"], hp["dropout"])
            self.model = FusionModel(enc, enc.out_dim, None, [], n_out, 0.0)
            self.model.load_state_dict(torch.load(self.dir / "model.pt", map_location="cpu"))
        else:
            raise PrepError(f"未知のファミリー: {self.family}")
        self.model.to(self.device).eval()

    # ---- 共通
    def required_columns(self) -> list[str]:
        return list(self.spec["text_cols"]) + list(self.spec["num_cols"]) + list(self.spec["cat_cols"])

    def predict_table(self, table: dict) -> dict:
        missing = [c for c in self.required_columns() if c not in table["columns"]]
        if missing:
            raise PrepError("予測に必要な列がありません: " + ", ".join(missing))
        examples = make_examples(table, self.spec, require_target=False)
        if self._impl is not None:
            preds, probs = self._impl.predict_examples(examples)
        else:
            preds, probs = self._predict_torch(examples)
        rows = []
        for k, e in enumerate(examples):
            item: dict = {"row": e["i"]}
            if self.task == "regression":
                item["pred"] = preds[k]
            else:
                item["pred"] = self.classes[preds[k]]
                item["prob"] = probs[k][preds[k]] if probs else None
                item["probs"] = probs[k] if probs else None
            rows.append(item)
        out = {"run_id": self.run_id, "task": self.task, "target": self.spec["target"],
               "classes": self.classes, "predictions": rows}
        if self.spec["target"] in table["columns"]:
            out["actual"] = self._actuals(table, examples)
        return out

    def _actuals(self, table: dict, examples: list[dict]) -> list:
        ti = table["columns"].index(self.spec["target"])
        vals = []
        for e in examples:
            v = table["rows"][e["i"]][ti]
            vals.append(None if is_missing(v) else str(v).strip())
        return vals

    def _predict_torch(self, examples: list[dict]):
        import torch

        feats = self._feature_tensors({"preproc": self.preproc}, examples)
        texts = [e["text"] for e in examples]
        emb = None
        if self.embedder is not None:
            emb = self.embedder.encode(texts)
        outs = []
        with torch.no_grad():
            for s in range(0, len(examples), PRED_BATCH):
                sl = slice(s, s + PRED_BATCH)
                b: dict = {}
                if self.text_collate is not None:
                    b.update(self.text_collate(texts[sl]))
                if emb is not None:
                    b["emb"] = emb[sl].to(self.device)
                if feats["num"] is not None:
                    b["num"] = feats["num"][sl].to(self.device)
                if feats["cat"] is not None:
                    b["cat"] = feats["cat"][sl].to(self.device)
                outs.append(self.model(b).float().cpu())
        if not outs:
            return [], None
        out = torch.cat(outs, dim=0)
        if self.task == "regression":
            return [self.preproc.target.inverse(v) for v in out.squeeze(-1).tolist()], None
        probs = torch.softmax(out, dim=-1)
        return probs.argmax(-1).tolist(), probs.tolist()
