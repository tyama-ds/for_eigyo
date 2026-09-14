"""torch 共通部品: 乱数固定、ヘッド、表形式特徴の埋め込み、融合モデル。"""
from __future__ import annotations

import random

import torch
from torch import nn

CAT_EMB_DIM = 8


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def parse_hidden(text) -> list[int]:
    if isinstance(text, (list, tuple)):
        return [int(x) for x in text]
    out = []
    for part in str(text or "").replace("，", ",").split(","):
        part = part.strip()
        if part:
            try:
                v = int(part)
            except ValueError:
                continue
            if v > 0:
                out.append(v)
    return out


def count_params(model: nn.Module, trainable_only: bool = False) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad or not trainable_only)


class MLPHead(nn.Module):
    def __init__(self, in_dim: int, hidden: list[int], out_dim: int, dropout: float = 0.1):
        super().__init__()
        layers: list[nn.Module] = []
        d = in_dim
        for h in hidden:
            layers += [nn.Linear(d, h), nn.GELU(), nn.Dropout(dropout)]
            d = h
        layers.append(nn.Linear(d, out_dim))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


class TabularFeatures(nn.Module):
    """数値列（標準化済み）とカテゴリ列（ID）をひとつのベクトルにする。"""

    def __init__(self, num_dim: int, cat_cards: list[int]):
        super().__init__()
        self.num_dim = num_dim
        self.embs = nn.ModuleList([nn.Embedding(c, CAT_EMB_DIM) for c in cat_cards])
        self.out_dim = num_dim + CAT_EMB_DIM * len(cat_cards)
        self.norm = nn.LayerNorm(self.out_dim) if self.out_dim else None

    def forward(self, num: torch.Tensor | None, cat: torch.Tensor | None):
        parts = []
        if self.num_dim and num is not None:
            parts.append(num)
        for j, emb in enumerate(self.embs):
            parts.append(emb(cat[:, j]))
        x = torch.cat(parts, dim=1)
        return self.norm(x) if self.norm is not None else x


class FusionModel(nn.Module):
    """encoder（テキスト/表形式）の出力と TabularFeatures を結合してヘッドへ渡す。

    encoder が None の場合は batch["emb"]（事前計算した埋め込み）をそのまま使う。
    """

    def __init__(self, encoder: nn.Module | None, enc_dim: int, tab: TabularFeatures | None,
                 head_hidden: list[int], out_dim: int, dropout: float):
        super().__init__()
        self.encoder = encoder
        self.tab = tab if (tab is not None and tab.out_dim > 0) else None
        in_dim = enc_dim + (self.tab.out_dim if self.tab is not None else 0)
        self.drop = nn.Dropout(dropout)
        self.head = MLPHead(in_dim, head_hidden, out_dim, dropout)

    def forward(self, batch: dict) -> torch.Tensor:
        if self.encoder is not None:
            x = self.encoder(batch)
        else:
            x = batch["emb"]
        if self.tab is not None:
            x = torch.cat([x, self.tab(batch.get("num"), batch.get("cat"))], dim=1)
        return self.head(self.drop(x))


def make_loss(task: str, hp: dict, class_w: list[float] | None, device) -> nn.Module:
    if task == "classification":
        w = torch.tensor(class_w, dtype=torch.float32, device=device) if class_w else None
        return nn.CrossEntropyLoss(weight=w)
    if hp.get("loss") == "huber":
        return nn.HuberLoss(delta=1.0)
    return nn.MSELoss()


def masked_mean(last_hidden: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    m = mask.unsqueeze(-1).to(last_hidden.dtype)
    return (last_hidden * m).sum(1) / m.sum(1).clamp(min=1e-6)
