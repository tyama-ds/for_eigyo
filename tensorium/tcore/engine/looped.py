"""Looped Transformer（再帰深さ / 重み共有ブロック）。

  prelude P（通常の層）→ 再帰コア R を r 回適用（重みは 1 組を共有、毎回入力 e を注入）→ coda C → プーリング

参考:
  - Universal Transformer (Dehghani+ 2018) / ALBERT (Lan+ 2019): 層間の重み共有
  - Giannou+ 2023 "Looped Transformers as Programmable Computers"
  - Saunshi+ ICLR 2025 "Reasoning with Latent Thoughts": k 層を L 回ループ ≈ kL 層の深いモデル
  - Geiping+ NeurIPS 2025 "Scaling up Test-Time Compute with Latent Reasoning": prelude / recurrent block / coda、
    入力注入、状態のランダム初期化、学習時のループ回数のランダム化、末尾 k 回だけの truncated backprop、
    推論時にループ回数を増やして test-time compute をスケール
  - Ouro (2025), Mixture-of-Recursions (2025), DeepLoop / Hyperloop (2026): ループ LM の大規模化・適応的終了

このモジュールは表データ（テキスト列 / 数値・カテゴリ列）向けの小型実装で、
学習後に「ループ回数 vs 検証指標」の曲線を出して推論時のループ回数を選べるようにする。
"""
from __future__ import annotations

import math
import random

import torch
from torch import nn

from .common import masked_mean
from .text import ScratchTokenizer


def _layers(d_model: int, nhead: int, ff_dim: int, dropout: float, n: int) -> nn.TransformerEncoder | None:
    if n <= 0:
        return None
    layer = nn.TransformerEncoderLayer(d_model, nhead, ff_dim, dropout, activation="gelu",
                                       batch_first=True, norm_first=True)
    return nn.TransformerEncoder(layer, n, enable_nested_tensor=False)


class LoopedCore(nn.Module):
    """prelude → (core × r, 入力注入) → coda。系列 (n, L, d) を受け取り (n, L, d) を返す。

    loops / loops_min: 学習時のループ回数（loops_min < loops なら毎ステップ一様ランダム）
    loops_eval:        推論時のループ回数（0 なら loops）。set_loops() で後から変更できる
    backprop_loops:    勾配を通す末尾のループ回数（0 なら全て）。前半は no_grad で走らせメモリを節約
    injection:         "concat"（[s; e] を線形で d に戻す。Geiping+ 方式）/ "add"（s + e）
    state_init:        "zeros" / "noise"（学習時のみ N(0, σ²) で初期化し、ループ回数への頑健性を上げる）
    """

    def __init__(self, d_model: int, nhead: int, ff_dim: int, dropout: float, prelude_layers: int,
                 core_layers: int, coda_layers: int, loops: int, loops_min: int = 0, loops_eval: int = 0,
                 backprop_loops: int = 0, injection: str = "concat", state_init: str = "zeros",
                 noise_std: float = 0.1):
        super().__init__()
        if d_model % nhead:
            raise ValueError(f"d_model({d_model}) はヘッド数({nhead})で割り切れる必要があります")
        if core_layers < 1:
            raise ValueError("再帰コアの層数は 1 以上にしてください")
        self.d_model = d_model
        self.loops = max(1, int(loops))
        self.loops_min = max(1, min(int(loops_min) or self.loops, self.loops))
        self.loops_eval = int(loops_eval) or self.loops
        self.backprop_loops = max(0, int(backprop_loops))
        self.injection = injection
        self.state_init = state_init
        self.noise_std = float(noise_std)
        self.prelude = _layers(d_model, nhead, ff_dim, dropout, prelude_layers)
        self.core = _layers(d_model, nhead, ff_dim, dropout, core_layers)
        self.coda = _layers(d_model, nhead, ff_dim, dropout, coda_layers)
        self.adapter = nn.Linear(2 * d_model, d_model) if injection == "concat" else None
        self.core_norm = nn.LayerNorm(d_model)
        self._rng = random.Random(0)
        self.last_loops = self.loops

    def set_loops(self, r: int) -> None:
        self.loops_eval = max(1, int(r))

    def sample_train_loops(self) -> int:
        if self.loops_min >= self.loops:
            return self.loops
        return self._rng.randint(self.loops_min, self.loops)

    def _inject(self, s: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        if self.adapter is not None:
            return self.adapter(torch.cat([s, e], dim=-1))
        return s + e

    def forward(self, x: torch.Tensor, pad_mask: torch.Tensor | None = None, loops: int | None = None) -> torch.Tensor:
        e = self.prelude(x, src_key_padding_mask=pad_mask) if self.prelude is not None else x
        r = int(loops) if loops else (self.sample_train_loops() if self.training else self.loops_eval)
        self.last_loops = r
        if self.state_init == "noise" and self.training:
            s = torch.randn_like(e) * self.noise_std
        else:
            s = torch.zeros_like(e)
        n_grad = self.backprop_loops if (self.training and 0 < self.backprop_loops < r) else r
        for i in range(r):
            if self.training and i < r - n_grad:
                with torch.no_grad():
                    s = self.core_norm(self.core(self._inject(s, e), src_key_padding_mask=pad_mask))
            else:
                s = self.core_norm(self.core(self._inject(s, e), src_key_padding_mask=pad_mask))
        if self.coda is not None:
            s = self.coda(s, src_key_padding_mask=pad_mask)
        return s


class LoopedTextEncoder(nn.Module):
    """文字 / 単語トークン列 → LoopedCore → プーリング。ScratchEncoder のループ版。"""

    def __init__(self, vocab_size: int, d_model: int, nhead: int, ff_dim: int, dropout: float, max_len: int,
                 pooling: str = "cls", **core_kw):
        super().__init__()
        self.d_model = d_model
        self.pooling = pooling
        self.tok_emb = nn.Embedding(vocab_size, d_model, padding_idx=0)
        self.pos_emb = nn.Embedding(max_len + 1, d_model)
        self.drop = nn.Dropout(dropout)
        self.core = LoopedCore(d_model, nhead, ff_dim, dropout, **core_kw)
        self.norm = nn.LayerNorm(d_model)
        self.out_dim = d_model

    def set_loops(self, r: int) -> None:
        self.core.set_loops(r)

    @property
    def loops_eval(self) -> int:
        return self.core.loops_eval

    def forward(self, batch: dict) -> torch.Tensor:
        ids = batch["input_ids"]
        mask = ids != ScratchTokenizer.PAD
        pos = torch.arange(ids.size(1), device=ids.device).unsqueeze(0)
        x = self.tok_emb(ids) * math.sqrt(self.d_model) + self.pos_emb(pos)
        x = self.core(self.drop(x), pad_mask=~mask, loops=batch.get("loops"))
        x = self.norm(x)
        if self.pooling == "mean":
            return masked_mean(x, mask)
        return x[:, 0]


class LoopedFTTransformer(nn.Module):
    """FT-Transformer の列トークン化 + LoopedCore（表形式データ向けの looped 版）。"""

    def __init__(self, num_dim: int, cat_cards: list[int], d_token: int, nhead: int, dropout: float, **core_kw):
        super().__init__()
        if num_dim + len(cat_cards) == 0:
            raise ValueError("数値またはカテゴリの列が 1 つ以上必要です")
        self.num_dim = num_dim
        self.num_w = nn.Parameter(torch.randn(num_dim, d_token) * 0.02) if num_dim else None
        self.num_b = nn.Parameter(torch.zeros(num_dim, d_token)) if num_dim else None
        offsets, total = [], 0
        for c in cat_cards:
            offsets.append(total)
            total += c
        self.cat_emb = nn.Embedding(total, d_token) if total else None
        self.register_buffer("offsets", torch.tensor(offsets, dtype=torch.long), persistent=False)
        self.cls = nn.Parameter(torch.randn(1, 1, d_token) * 0.02)
        self.core = LoopedCore(d_token, nhead, d_token * 2, dropout, **core_kw)
        self.norm = nn.LayerNorm(d_token)
        self.out_dim = d_token

    def set_loops(self, r: int) -> None:
        self.core.set_loops(r)

    @property
    def loops_eval(self) -> int:
        return self.core.loops_eval

    def forward(self, batch: dict) -> torch.Tensor:
        tokens = []
        if self.num_dim:
            tokens.append(batch["num"].unsqueeze(-1) * self.num_w + self.num_b)
        if self.cat_emb is not None:
            tokens.append(self.cat_emb(batch["cat"] + self.offsets))
        x = torch.cat(tokens, dim=1)
        x = torch.cat([self.cls.expand(x.size(0), -1, -1), x], dim=1)
        x = self.core(x, loops=batch.get("loops"))
        return self.norm(x[:, 0])


def loop_core_kwargs(hp: dict) -> dict:
    """ハイパーパラメータ辞書から LoopedCore の引数を取り出す。"""
    return {
        "prelude_layers": int(hp.get("prelude_layers", 1)), "core_layers": int(hp.get("core_layers", 1)),
        "coda_layers": int(hp.get("coda_layers", 1)), "loops": int(hp.get("loops", 6)),
        "loops_min": int(hp.get("loops_min", 0)), "loops_eval": int(hp.get("loops_eval", 0)),
        "backprop_loops": int(hp.get("backprop_loops", 0)), "injection": hp.get("injection", "concat"),
        "state_init": hp.get("state_init", "zeros"), "noise_std": float(hp.get("noise_std", 0.1)),
    }
