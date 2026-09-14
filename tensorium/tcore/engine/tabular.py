"""FT-Transformer: 各列を 1 トークンに埋め込み、自己注意で列間の相互作用を学習する。"""
from __future__ import annotations

import torch
from torch import nn


class FTTransformer(nn.Module):
    def __init__(self, num_dim: int, cat_cards: list[int], d_token: int, nhead: int, layers: int,
                 dropout: float):
        super().__init__()
        if num_dim + len(cat_cards) == 0:
            raise ValueError("数値またはカテゴリの列が 1 つ以上必要です")
        if d_token % nhead:
            raise ValueError(f"d_token({d_token}) はヘッド数({nhead})で割り切れる必要があります")
        self.num_dim = num_dim
        self.num_w = nn.Parameter(torch.randn(num_dim, d_token) * 0.02) if num_dim else None
        self.num_b = nn.Parameter(torch.zeros(num_dim, d_token)) if num_dim else None
        self.cat_offsets = []
        total = 0
        for c in cat_cards:
            self.cat_offsets.append(total)
            total += c
        self.cat_emb = nn.Embedding(total, d_token) if total else None
        self.register_buffer("offsets", torch.tensor(self.cat_offsets, dtype=torch.long), persistent=False)
        self.cls = nn.Parameter(torch.randn(1, 1, d_token) * 0.02)
        layer = nn.TransformerEncoderLayer(d_token, nhead, d_token * 2, dropout, activation="gelu",
                                           batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(layer, layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(d_token)
        self.out_dim = d_token

    def forward(self, batch: dict) -> torch.Tensor:
        tokens = []
        if self.num_dim:
            num = batch["num"]                                       # (n, num_dim)
            tokens.append(num.unsqueeze(-1) * self.num_w + self.num_b)   # (n, num_dim, d)
        if self.cat_emb is not None:
            cat = batch["cat"] + self.offsets                         # (n, n_cat)
            tokens.append(self.cat_emb(cat))
        x = torch.cat(tokens, dim=1)
        cls = self.cls.expand(x.size(0), -1, -1)
        x = self.encoder(torch.cat([cls, x], dim=1))
        return self.norm(x[:, 0])
