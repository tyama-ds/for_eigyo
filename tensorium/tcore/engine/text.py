"""テキストエンコーダ: Hugging Face 事前学習モデル / ゼロから学習する Transformer。"""
from __future__ import annotations

import math
import re
from collections import Counter

import torch
from torch import nn

from .common import masked_mean

_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")
_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class ModelLoadError(Exception):
    """モデル/トークナイザの読み込み失敗（原因と対処を含むメッセージ）。"""


def explain_load_error(e: Exception, model_path: str) -> str:
    msg = str(e)
    low = msg.lower()
    if "fugashi" in low or "mecab" in low:
        return (f"{model_path} のトークナイザには MeCab 系ライブラリが必要です: "
                "pip install fugashi unidic-lite（旧モデルは ipadic）")
    if "sentencepiece" in low:
        return f"{model_path} のトークナイザには sentencepiece が必要です: pip install sentencepiece"
    if "trust_remote_code" in low:
        return f"{model_path} はカスタムコードを含みます。設定で「リモートコードを信頼」を ON にしてください"
    if any(k in low for k in ("couldn't connect", "connection", "offline", "proxy", "max retries",
                              "not a local folder", "is not a valid model identifier", "403", "401")):
        return (f"{model_path} を取得できません。(1) モデル ID の綴り (2) プロキシ / HF ミラー設定 "
                f"(3) 事前ダウンロード済みのローカルフォルダを指定 のいずれかを確認してください。原因: {msg[:300]}")
    return f"{model_path} の読み込みに失敗: {msg[:400]}"


# ---------------------------------------------------------------- Hugging Face

def load_hf_tokenizer(model_path: str, trust_remote_code: bool = False):
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(model_path, trust_remote_code=trust_remote_code)
    except Exception as e:  # noqa: BLE001
        raise ModelLoadError(explain_load_error(e, model_path)) from e


def load_hf_model(model_path: str, trust_remote_code: bool = False):
    try:
        from transformers import AutoModel
        return AutoModel.from_pretrained(model_path, trust_remote_code=trust_remote_code)
    except Exception as e:  # noqa: BLE001
        raise ModelLoadError(explain_load_error(e, model_path)) from e


class HFEncoder(nn.Module):
    def __init__(self, model, pooling: str = "cls", freeze: bool = False):
        super().__init__()
        self.model = model
        self.pooling = pooling
        self.out_dim = int(getattr(model.config, "hidden_size", None) or model.config.d_model)
        if freeze:
            for p in self.model.parameters():
                p.requires_grad_(False)

    def forward(self, batch: dict) -> torch.Tensor:
        kwargs = {"input_ids": batch["input_ids"], "attention_mask": batch["attention_mask"]}
        if "token_type_ids" in batch:
            kwargs["token_type_ids"] = batch["token_type_ids"]
        out = self.model(**kwargs)
        last = out.last_hidden_state
        if self.pooling == "mean":
            return masked_mean(last, batch["attention_mask"])
        return last[:, 0]


class HFCollate:
    """テキストをその場でトークナイズしてバッチにする。"""

    def __init__(self, tokenizer, max_len: int, device):
        self.tok = tokenizer
        self.max_len = max_len
        self.device = device

    def __call__(self, texts: list[str]) -> dict:
        enc = self.tok(texts, padding=True, truncation=True, max_length=self.max_len, return_tensors="pt")
        out = {"input_ids": enc["input_ids"].to(self.device),
               "attention_mask": enc["attention_mask"].to(self.device)}
        if "token_type_ids" in enc:
            out["token_type_ids"] = enc["token_type_ids"].to(self.device)
        return out


# ---------------------------------------------------------------- ゼロから学習

class ScratchTokenizer:
    PAD, UNK, CLS, SEP = 0, 1, 2, 3
    SPECIALS = ["<pad>", "<unk>", "<cls>", "<sep>"]

    def __init__(self, mode: str = "char", vocab: list[str] | None = None):
        self.mode = mode
        self.itos = list(vocab) if vocab else list(self.SPECIALS)
        self.stoi = {t: i for i, t in enumerate(self.itos)}

    @staticmethod
    def detect_mode(texts: list[str]) -> str:
        sample = texts[:2000]
        if not sample:
            return "char"
        cjk = sum(1 for t in sample if _CJK_RE.search(t))
        return "char" if cjk / len(sample) >= 0.2 else "word"

    def _split(self, text: str) -> list[str]:
        text = text.strip()
        if self.mode == "word":
            return [w.lower() for w in _WORD_RE.findall(text)]
        return [ch for ch in text if not ch.isspace()]

    def fit(self, texts: list[str], vocab_size: int = 8000) -> ScratchTokenizer:
        cnt: Counter = Counter()
        for t in texts:
            cnt.update(self._split(t))
        keep = [tok for tok, _ in cnt.most_common(max(vocab_size - len(self.SPECIALS), 1))]
        self.itos = list(self.SPECIALS) + keep
        self.stoi = {t: i for i, t in enumerate(self.itos)}
        return self

    def encode(self, text: str, max_len: int) -> list[int]:
        ids = [self.stoi.get(tok, self.UNK) for tok in self._split(text)]
        return [self.CLS] + ids[: max(max_len - 1, 1)]

    @property
    def vocab_size(self) -> int:
        return len(self.itos)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "vocab": self.itos}

    @classmethod
    def from_dict(cls, d: dict) -> ScratchTokenizer:
        return cls(d["mode"], d["vocab"])


class ScratchCollate:
    def __init__(self, tokenizer: ScratchTokenizer, max_len: int, device):
        self.tok = tokenizer
        self.max_len = max_len
        self.device = device

    def __call__(self, texts: list[str]) -> dict:
        seqs = [self.tok.encode(t, self.max_len) for t in texts]
        width = max(len(s) for s in seqs)
        ids = torch.full((len(seqs), width), ScratchTokenizer.PAD, dtype=torch.long)
        for i, s in enumerate(seqs):
            ids[i, : len(s)] = torch.tensor(s, dtype=torch.long)
        return {"input_ids": ids.to(self.device)}


class ScratchEncoder(nn.Module):
    """学習可能な位置埋め込み + Pre-LN Transformer エンコーダ。"""

    def __init__(self, vocab_size: int, d_model: int, nhead: int, layers: int, ff_dim: int,
                 dropout: float, max_len: int, pooling: str = "cls"):
        super().__init__()
        if d_model % nhead:
            raise ValueError(f"d_model({d_model}) はヘッド数({nhead})で割り切れる必要があります")
        self.d_model = d_model
        self.pooling = pooling
        self.tok_emb = nn.Embedding(vocab_size, d_model, padding_idx=0)
        self.pos_emb = nn.Embedding(max_len + 1, d_model)
        layer = nn.TransformerEncoderLayer(d_model, nhead, ff_dim, dropout, activation="gelu",
                                           batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(layer, layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(d_model)
        self.drop = nn.Dropout(dropout)
        self.out_dim = d_model

    def forward(self, batch: dict) -> torch.Tensor:
        ids = batch["input_ids"]
        mask = ids != ScratchTokenizer.PAD
        pos = torch.arange(ids.size(1), device=ids.device).unsqueeze(0)
        x = self.tok_emb(ids) * math.sqrt(self.d_model) + self.pos_emb(pos)
        x = self.encoder(self.drop(x), src_key_padding_mask=~mask)
        x = self.norm(x)
        if self.pooling == "mean":
            return masked_mean(x, mask)
        return x[:, 0]
