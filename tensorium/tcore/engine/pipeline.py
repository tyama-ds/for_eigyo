"""torch 系ファミリー（hf / sbert / scratch / tabular）の学習パイプライン。"""
from __future__ import annotations

import math
import random
import time

import torch

from .. import metrics as M
from ..config import load_settings
from ..env import resolve_device
from ..prep import PrepError
from ..runs import new_run_id, run_dir, save_meta, write_json
from .common import FusionModel, TabularFeatures, count_params, make_loss, parse_hidden, set_seed
from .datasetup import build_meta, evaluate_split, prepare
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

EVAL_BATCH_MULT = 2


# ---------------------------------------------------------------- テンソル化

def feature_tensors(bundle: dict, examples: list[dict] | None = None) -> dict:
    """数値/カテゴリ列を (n, k) テンソルにする（無い場合は None）。"""
    pre = bundle["preproc"]
    ex = examples if examples is not None else bundle["examples"]
    spec = pre.spec
    out = {"num": None, "cat": None}
    if spec["num_cols"]:
        out["num"] = torch.tensor([pre.num.transform(e["num"]) for e in ex], dtype=torch.float32)
    if spec["cat_cols"]:
        out["cat"] = torch.tensor([pre.cat.transform(e["cat"]) for e in ex], dtype=torch.long)
    return out


def make_tab(bundle: dict) -> TabularFeatures | None:
    spec = bundle["preproc"].spec
    num_dim = len(spec["num_cols"])
    cards = bundle["preproc"].cat.cardinalities if spec["cat_cols"] else []
    if num_dim == 0 and not cards:
        return None
    return TabularFeatures(num_dim, cards)


class BatchMaker:
    def __init__(self, texts, text_collate, emb, feats: dict, y, device):
        self.texts = texts
        self.text_collate = text_collate
        self.emb = emb
        self.num = feats.get("num")
        self.cat = feats.get("cat")
        self.y = y
        self.device = device

    def __call__(self, idx: list[int]) -> dict:
        it = torch.tensor(idx, dtype=torch.long)
        b: dict = {}
        if self.text_collate is not None:
            b.update(self.text_collate([self.texts[i] for i in idx]))
        if self.emb is not None:
            b["emb"] = self.emb[it].to(self.device)
        if self.num is not None:
            b["num"] = self.num[it].to(self.device)
        if self.cat is not None:
            b["cat"] = self.cat[it].to(self.device)
        if self.y is not None:
            b["y"] = self.y[it].to(self.device)
        return b


# ---------------------------------------------------------------- 学習ループ

class Trainer:
    def __init__(self, model, bundle: dict, device: str, job, make_batch, loss_fn):
        self.model = model
        self.b = bundle
        self.task = bundle["task"]
        self.hp = bundle["hparams"]
        self.device = device
        self.job = job
        self.make_batch = make_batch
        self.loss_fn = loss_fn
        self.use_amp = bool(self.hp.get("fp16")) and device == "cuda"

    def _loss(self, out, y):
        if self.task == "regression":
            return self.loss_fn(out.squeeze(-1).float(), y)
        return self.loss_fn(out.float(), y)

    @torch.no_grad()
    def predict(self, idx: list[int], with_loss: bool = True):
        """→ (preds, probs, mean_loss)。回帰の preds は元スケール。"""
        self.model.eval()
        bs = max(1, int(self.hp["batch_size"])) * EVAL_BATCH_MULT
        outs, losses = [], []
        for s in range(0, len(idx), bs):
            self.job.check_cancel()
            batch = self.make_batch(idx[s:s + bs])
            out = self.model(batch)
            if with_loss and "y" in batch:
                losses.append(self._loss(out, batch["y"]).item() * len(idx[s:s + bs]))
            outs.append(out.detach().float().cpu())
        out = torch.cat(outs, dim=0) if outs else torch.zeros((0, 1))
        mean_loss = (sum(losses) / len(idx)) if losses else None
        if self.task == "regression":
            inv = self.b["preproc"].target.inverse
            return [inv(v) for v in out.squeeze(-1).tolist()], None, mean_loss
        probs = torch.softmax(out, dim=-1)
        return probs.argmax(-1).tolist(), probs.tolist(), mean_loss

    def _epoch_metrics(self, idx):
        preds, probs, loss = self.predict(idx)
        ev = evaluate_split(self.b, idx, preds, probs)
        return ev["metrics"] if ev else {}, loss

    def fit(self, train_idx: list[int], val_idx: list[int]) -> dict:
        hp, job = self.hp, self.job
        bs = max(1, int(hp["batch_size"]))
        epochs = int(hp["epochs"])
        steps_per_epoch = max(1, math.ceil(len(train_idx) / bs))
        total = steps_per_epoch * epochs
        warmup = int(total * float(hp.get("warmup_ratio", 0.0)))
        params = [p for p in self.model.parameters() if p.requires_grad]
        if not params:
            raise PrepError("学習対象のパラメータがありません（エンコーダ凍結時はヘッドが必要）")
        opt = torch.optim.AdamW(params, lr=float(hp["lr"]), weight_decay=float(hp.get("weight_decay", 0.0)))

        def lr_lambda(step):
            if warmup and step < warmup:
                return (step + 1) / warmup
            return max(0.02, (total - step) / max(1, total - warmup))

        sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda)
        scaler = torch.amp.GradScaler("cuda") if self.use_amp else None
        clip = float(hp.get("grad_clip", 0.0) or 0.0)
        patience = int(hp.get("early_stopping", 0) or 0)
        rng = random.Random(self.b["spec"]["split"]["seed"])
        best_score, best_epoch, best_state, bad = -float("inf"), 0, None, 0
        t0 = time.time()
        gstep = 0
        log_every = max(1, steps_per_epoch // 20)
        epoch = 0
        job.log(f"学習開始: {epochs} エポック × {steps_per_epoch} ステップ（バッチ {bs}）")
        for epoch in range(1, epochs + 1):
            self.model.train()
            order = list(train_idx)
            rng.shuffle(order)
            losses = []
            for s in range(steps_per_epoch):
                job.check_cancel()
                idx = order[s * bs:(s + 1) * bs]
                if not idx:
                    break
                batch = self.make_batch(idx)
                opt.zero_grad(set_to_none=True)
                if scaler is not None:
                    with torch.autocast("cuda", dtype=torch.float16):
                        out = self.model(batch)
                        loss = self._loss(out, batch["y"])
                    scaler.scale(loss).backward()
                    scaler.unscale_(opt)
                    if clip > 0:
                        torch.nn.utils.clip_grad_norm_(params, clip)
                    scaler.step(opt)
                    scaler.update()
                else:
                    out = self.model(batch)
                    loss = self._loss(out, batch["y"])
                    loss.backward()
                    if clip > 0:
                        torch.nn.utils.clip_grad_norm_(params, clip)
                    opt.step()
                sched.step()
                gstep += 1
                lv = float(loss.item())
                if not math.isfinite(lv):
                    raise PrepError("損失が発散しました（NaN/Inf）。学習率を下げるか、"
                                    "目的変数の外れ値を確認してください")
                losses.append(lv)
                job.step_loss(gstep, lv)
                if s % log_every == 0 or s == steps_per_epoch - 1:
                    elapsed = time.time() - t0
                    eta = elapsed / gstep * (total - gstep) if gstep else None
                    job.update(pct=min(99.0, 100.0 * gstep / total), phase=f"学習中 epoch {epoch}/{epochs}",
                               epoch=epoch, epochs=epochs, step=s + 1, steps=steps_per_epoch, loss=lv,
                               eta_sec=eta, lr=sched.get_last_lr()[0], global_step=gstep, total_steps=total)
            train_loss = sum(losses) / max(len(losses), 1)
            record = {"epoch": epoch, "train_loss": train_loss, "lr": sched.get_last_lr()[0],
                      "elapsed": time.time() - t0}
            if val_idx:
                m, val_loss = self._epoch_metrics(val_idx)
                record["val_loss"] = val_loss
                for k in ("rmse", "mae", "r2", "accuracy", "f1_macro", "f1_weighted", "log_loss"):
                    if k in m and m[k] is not None:
                        record[k] = m[k]
                name, score = M.primary_metric(self.task, m)
                msg = f"epoch {epoch}: train_loss={train_loss:.4f} val_loss={val_loss:.4f} {name}={abs(score):.4f}"
            else:
                score = -train_loss
                msg = f"epoch {epoch}: train_loss={train_loss:.4f}（検証データなし）"
            job.epoch_done(record)
            job.log(msg)
            if score is not None and score > best_score + 1e-9:
                best_score, best_epoch, bad = score, epoch, 0
                best_state = {k: v.detach().to("cpu", copy=True) for k, v in self.model.state_dict().items()}
            else:
                bad += 1
                if patience and bad >= patience:
                    job.log(f"早期終了: {patience} エポック改善なし（ベスト epoch {best_epoch}）")
                    break
        if best_state is not None:
            self.model.load_state_dict(best_state)
        return {"best_epoch": best_epoch, "epochs_run": epoch, "train_seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- ファミリー別の構築

def _texts(bundle):
    return [e["text"] for e in bundle["examples"]]


def build_model(bundle: dict, device: str, cfg: dict, job):
    """→ (model, text_collate, emb, arch, saver)"""
    hp = bundle["hparams"]
    fam = bundle["family"]
    n_out = bundle["n_out"]
    trust = bool(cfg.get("trust_remote_code"))
    tab = make_tab(bundle)
    arch: dict = {"family": fam, "task": bundle["task"], "n_out": n_out, "hparams": hp,
                  "model_path": bundle["model_id"]}

    if fam == "hf":
        job.update(phase="モデルを読み込み中", pct=2.0)
        job.log(f"事前学習モデルを読み込み: {bundle['model_id']}")
        tok = load_hf_tokenizer(bundle["model_id"], trust)
        base = load_hf_model(bundle["model_id"], trust)
        enc = HFEncoder(base, hp["pooling"], hp["freeze_encoder"])
        model = FusionModel(enc, enc.out_dim, tab, parse_hidden(hp["head_hidden"]), n_out, hp["dropout"])
        collate = HFCollate(tok, hp["max_len"], device)
        arch.update({"enc_dim": enc.out_dim, "pooling": hp["pooling"]})

        def saver(d):
            enc.model.save_pretrained(d / "encoder")
            tok.save_pretrained(d / "encoder")
            head = {k: v for k, v in model.state_dict().items() if not k.startswith("encoder.model.")}
            torch.save(head, d / "head.pt")
        return model, collate, None, arch, saver

    if fam == "sbert":
        job.update(phase="埋め込みモデルを読み込み中", pct=2.0)
        job.log(f"文埋め込みモデルを読み込み: {bundle['model_id']}")
        embedder = SentenceEmbedder(bundle["model_id"], hp["max_len"], device, hp["normalize"],
                                    hp["embed_batch"], trust, job.log)
        texts = _texts(bundle)
        job.update(phase="テキストを埋め込み中", pct=5.0)
        t0 = time.time()
        emb = embedder.encode(texts, progress=lambda done, tot: job.update(
            pct=5.0 + 25.0 * done / max(tot, 1), phase=f"テキストを埋め込み中 {done}/{tot}"))
        job.log(f"埋め込み完了: {len(texts)} 件 / {embedder.dim} 次元 / {time.time() - t0:.1f} 秒")
        model = FusionModel(None, embedder.dim, tab, parse_hidden(hp["head_hidden"]), n_out, hp["dropout"])
        arch.update({"enc_dim": embedder.dim, "backend": embedder.backend})

        def saver(d):
            torch.save(model.state_dict(), d / "model.pt")
        return model, None, emb, arch, saver

    if fam == "scratch":
        train_texts = [bundle["examples"][i]["text"] for i in bundle["split"]["train"]]
        mode = hp["tokenizer"] if hp["tokenizer"] != "auto" else ScratchTokenizer.detect_mode(train_texts)
        tokenizer = ScratchTokenizer(mode).fit(train_texts, hp["vocab_size"])
        job.log(f"トークナイザ: {'文字単位' if mode == 'char' else '単語単位'} / 語彙 {tokenizer.vocab_size}")
        try:
            enc = ScratchEncoder(tokenizer.vocab_size, hp["d_model"], hp["nhead"], hp["layers"], hp["ff_dim"],
                                 hp["dropout"], hp["max_len"], hp["pooling"])
        except ValueError as e:
            raise PrepError(str(e)) from e
        model = FusionModel(enc, enc.out_dim, tab, [], n_out, hp["dropout"])
        collate = ScratchCollate(tokenizer, hp["max_len"], device)
        arch.update({"enc_dim": enc.out_dim, "tokenizer": tokenizer.to_dict()})

        def saver(d):
            torch.save(model.state_dict(), d / "model.pt")
        return model, collate, None, arch, saver

    if fam == "tabular":
        spec = bundle["spec"]
        cards = bundle["preproc"].cat.cardinalities if spec["cat_cols"] else []
        try:
            enc = FTTransformer(len(spec["num_cols"]), cards, hp["d_token"], hp["nhead"], hp["layers"], hp["dropout"])
        except ValueError as e:
            raise PrepError(str(e)) from e
        model = FusionModel(enc, enc.out_dim, None, [], n_out, hp["dropout"])
        arch.update({"enc_dim": enc.out_dim, "num_dim": len(spec["num_cols"]), "cat_cards": cards})

        def saver(d):
            torch.save(model.state_dict(), d / "model.pt")
        return model, None, None, arch, saver

    raise PrepError(f"このファミリーは torch パイプラインでは扱えません: {fam}")


# ---------------------------------------------------------------- エントリ

def train_run(table: dict, req: dict, job) -> dict:
    started = time.time()
    cfg = load_settings()
    b = prepare(table, req, job.log)
    device = resolve_device(cfg.get("device", "auto"))
    if cfg.get("num_threads"):
        torch.set_num_threads(int(cfg["num_threads"]))
    set_seed(b["spec"]["split"]["seed"])
    job.log(f"デバイス: {device} / ファミリー: {b['family_info']['name']}")

    model, text_collate, emb, arch, saver = build_model(b, device, cfg, job)
    model.to(device)
    n_params = count_params(model)
    n_train = count_params(model, trainable_only=True)
    job.log(f"パラメータ数: {n_params:,}（学習対象 {n_train:,}）")

    feats = feature_tensors(b)
    if b["task"] == "regression":
        y = torch.tensor(b["y_enc"], dtype=torch.float32)
    else:
        y = torch.tensor([max(v, 0) for v in b["y_enc"]], dtype=torch.long)
    make_batch = BatchMaker(_texts(b), text_collate, emb, feats, y, device)
    loss_fn = make_loss(b["task"], b["hparams"], b["class_weights"], device)
    trainer = Trainer(model, b, device, job, make_batch, loss_fn)
    fit_info = trainer.fit(b["split"]["train"], b["split"]["val"])

    job.update(phase="評価中", pct=99.0)
    evals = {}
    for part in ("val", "test"):
        idx = b["split"][part]
        if idx:
            preds, probs, _ = trainer.predict(idx, with_loss=False)
            evals[part] = evaluate_split(b, idx, preds, probs)
        else:
            evals[part] = None
    if evals["val"] is None and evals["test"] is None:      # 検証もテストも無い: 学習データで参考評価
        idx = b["split"]["train"]
        preds, probs, _ = trainer.predict(idx, with_loss=False)
        evals["val"] = evaluate_split(b, idx, preds, probs)
        job.log("検証/テストが 0 件のため、学習データでの参考評価を val として保存")

    run_id = new_run_id()
    d = run_dir(run_id, create=True)
    job.update(phase="保存中")
    saver(d)
    write_json(d / "preproc.json", b["preproc"].to_dict())
    write_json(d / "arch.json", arch)
    extra = {"eval": evals, "curves": job.curves, "step_losses": job.step_losses, "device": device,
             "n_params": n_params, "n_trainable": n_train, **fit_info}
    meta = build_meta(b, run_id, req, extra, started)
    save_meta(run_id, meta)
    job.update(pct=100.0, phase="完了")
    job.log(f"保存: {run_id}（{meta['duration_sec']} 秒）")
    return {"run_id": run_id, "metrics": meta["metrics"], "primary_metric": meta["primary_metric"]}
