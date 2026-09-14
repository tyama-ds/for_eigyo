"""Explicit SBERT presets; saved selections cannot be changed by environment variables."""

import os


SBERT_MODELS = {
    "multilingual_minilm": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "mpnet": "sentence-transformers/all-mpnet-base-v2",
}
DEFAULT_SBERT_MODEL = "mpnet"
LEGACY_TRANSFORMER_MODEL = SBERT_MODELS["multilingual_minilm"]


def resolve_embedding_model(embedding: str, sbert_model: str | None = None) -> dict:
    """Return an auditable model ID and preset without loading optional packages."""
    if embedding == "tfidf":
        return {"model_id": "TF-IDF / unigrams + bigrams", "preset": None,
                "legacy_environment_override": False}
    if embedding == "transformer":
        override = os.environ.get("ATLAS_EMBEDDING_MODEL", "").strip()
        return {"model_id": override or LEGACY_TRANSFORMER_MODEL,
                "preset": "legacy_custom" if override else "multilingual_minilm",
                "legacy_environment_override": bool(override)}
    if embedding != "sbert":
        raise ValueError("文書表現は tfidf・sbert・transformer を指定してください。")
    selected = DEFAULT_SBERT_MODEL if sbert_model is None else sbert_model
    if not isinstance(selected, str) or selected not in SBERT_MODELS:
        raise ValueError("SBERT モデルは multilingual_minilm または mpnet を指定してください。")
    return {"model_id": SBERT_MODELS[selected], "preset": selected,
            "legacy_environment_override": False}
