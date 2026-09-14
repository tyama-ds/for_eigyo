"""SentenceBERT 系の文埋め込み（sentence-transformers があれば使用、無ければ transformers の平均プーリング）。"""
from __future__ import annotations

import torch

from .common import masked_mean
from .text import ModelLoadError, explain_load_error, load_hf_model, load_hf_tokenizer

CHUNK = 256


class SentenceEmbedder:
    def __init__(self, model_path: str, max_len: int, device: str, normalize: bool = True,
                 batch_size: int = 32, trust_remote_code: bool = False, log=None):
        self.model_path = model_path
        self.max_len = max_len
        self.device = device
        self.normalize = normalize
        self.batch_size = max(1, int(batch_size))
        self.backend = "transformers"
        self._st = None
        self._tok = None
        self._model = None
        try:
            from sentence_transformers import SentenceTransformer
            try:
                self._st = SentenceTransformer(model_path, device=device, trust_remote_code=trust_remote_code)
            except Exception as e:  # noqa: BLE001
                raise ModelLoadError(explain_load_error(e, model_path)) from e
            self._st.max_seq_length = max_len
            self.backend = "sentence-transformers"
            self.dim = int(self._st.get_sentence_embedding_dimension() or 0)
        except ImportError:
            self._tok = load_hf_tokenizer(model_path, trust_remote_code)
            self._model = load_hf_model(model_path, trust_remote_code).to(device).eval()
            self.dim = int(self._model.config.hidden_size)
        if log:
            log(f"埋め込みバックエンド: {self.backend} / 次元 {self.dim}")

    @torch.no_grad()
    def _encode_chunk(self, texts: list[str]) -> torch.Tensor:
        if self._st is not None:
            emb = self._st.encode(texts, batch_size=self.batch_size, convert_to_tensor=True,
                                  normalize_embeddings=self.normalize, show_progress_bar=False)
            return emb.detach().float().cpu()
        outs = []
        for i in range(0, len(texts), self.batch_size):
            enc = self._tok(texts[i:i + self.batch_size], padding=True, truncation=True,
                            max_length=self.max_len, return_tensors="pt").to(self.device)
            last = self._model(**enc).last_hidden_state
            pooled = masked_mean(last, enc["attention_mask"])
            if self.normalize:
                pooled = torch.nn.functional.normalize(pooled, dim=-1)
            outs.append(pooled.float().cpu())
        return torch.cat(outs, dim=0)

    def encode(self, texts: list[str], progress=None) -> torch.Tensor:
        """texts 全体を埋め込む。progress(done, total) を呼びながら進める。"""
        if not texts:
            return torch.zeros((0, self.dim))
        parts = []
        for i in range(0, len(texts), CHUNK):
            parts.append(self._encode_chunk(texts[i:i + CHUNK]))
            if progress:
                progress(min(i + CHUNK, len(texts)), len(texts))
        return torch.cat(parts, dim=0)
