"""モデルファミリー・プリセット・ハイパーパラメータ定義（UI はこれを読んでフォームを組む）。"""
from __future__ import annotations

FAMILIES = [
    {
        "id": "hf",
        "name": "Transformer ファインチューニング",
        "short": "BERT / RoBERTa / DeBERTa / E5 …",
        "desc": "事前学習済み Transformer（BERT 系）全体を学習データで微調整する。テキスト列を "
                "エンコードし、数値・カテゴリ列があれば融合ヘッドで結合。精度は最も出やすいが CPU では時間がかかる。",
        "requires": ["torch", "transformers"],
        "needs_text": True,
        "downloads": True,
        "color": "violet",
        "speed": "遅い（GPU 推奨）",
        "accuracy": "高",
    },
    {
        "id": "sbert",
        "name": "SentenceBERT 埋め込み + MLP",
        "short": "文埋め込みを固定して小さなヘッドを学習",
        "desc": "SentenceBERT / E5 などで各行のテキストを一度だけ埋め込みベクトルに変換し、"
                "その上に多層パーセプトロンを学習する。エンコーダは更新しないので CPU でも速く、"
                "少データでも安定。数値・カテゴリ列も結合できる。",
        "requires": ["torch", "transformers"],
        "optional": ["sentence_transformers"],
        "needs_text": True,
        "downloads": True,
        "color": "cyan",
        "speed": "速い",
        "accuracy": "中〜高",
    },
    {
        "id": "scratch",
        "name": "Transformer をゼロから学習",
        "short": "事前学習なし・ダウンロード不要",
        "desc": "文字（または単語）トークナイザを学習データから作り、小さな Transformer エンコーダを"
                "ゼロから学習する。モデルのダウンロードが不要でオフライン環境でも動く。"
                "Transformer の挙動を確かめたい時や、専門用語・記号中心のテキストに向く。",
        "requires": ["torch"],
        "needs_text": True,
        "downloads": False,
        "color": "amber",
        "speed": "速い",
        "accuracy": "データ量に依存",
    },
    {
        "id": "tabular",
        "name": "FT-Transformer（表形式）",
        "short": "数値・カテゴリ列を Transformer で学習",
        "desc": "各列を 1 トークンとして埋め込み、列同士の相互作用を自己注意で学習する表形式データ向け "
                "Transformer。テキスト列は使わない（テキストがある場合は他のファミリーで融合）。",
        "requires": ["torch"],
        "needs_text": False,
        "downloads": False,
        "color": "green",
        "speed": "速い",
        "accuracy": "中〜高",
    },
    {
        "id": "baseline",
        "name": "ベースライン",
        "short": "平均値 / 最頻値",
        "desc": "回帰なら学習データの平均値、分類なら最頻クラスを常に予測する。"
                "他のモデルの指標が「当たり前の予測」よりどれだけ良いかを見る比較基準。依存ライブラリ不要。",
        "requires": [],
        "needs_text": False,
        "downloads": False,
        "color": "gray",
        "speed": "即時",
        "accuracy": "基準",
    },
]

# 事前学習済みモデルのプリセット。families は使えるファミリー。
PRESETS = [
    # ---- 日本語
    {"id": "tohoku-nlp/bert-base-japanese-v3", "name": "BERT base 日本語 v3（東北大）", "lang": "ja",
     "families": ["hf", "sbert"], "params": "111M", "note": "日本語 BERT の定番。MeCab 系トークナイザ",
     "extra_pip": ["fugashi", "unidic-lite"], "recommended": True},
    {"id": "tohoku-nlp/bert-base-japanese-char-v3", "name": "BERT base 日本語 文字単位 v3（東北大）", "lang": "ja",
     "families": ["hf", "sbert"], "params": "91M", "note": "文字単位。未知語・表記ゆれに強い",
     "extra_pip": ["fugashi", "unidic-lite"]},
    {"id": "ku-nlp/deberta-v2-base-japanese", "name": "DeBERTa-v2 base 日本語（京大）", "lang": "ja",
     "families": ["hf"], "params": "112M", "note": "高精度。sentencepiece 必要",
     "extra_pip": ["sentencepiece"]},
    {"id": "studio-ousia/luke-japanese-base-lite", "name": "LUKE 日本語 base lite", "lang": "ja",
     "families": ["hf"], "params": "133M", "note": "sentencepiece 必要", "extra_pip": ["sentencepiece"]},
    {"id": "cl-nagoya/ruri-v3-30m", "name": "Ruri v3 30M（名大・ModernBERT）", "lang": "ja",
     "families": ["sbert", "hf"], "params": "37M", "note": "軽量な日本語文埋め込み。transformers>=4.48",
     "recommended": True},
    {"id": "cl-nagoya/ruri-v3-130m", "name": "Ruri v3 130M（名大）", "lang": "ja",
     "families": ["sbert", "hf"], "params": "132M", "note": "日本語文埋め込みの高精度モデル。transformers>=4.48"},
    {"id": "cl-nagoya/sup-simcse-ja-base", "name": "Sup-SimCSE 日本語 base（名大）", "lang": "ja",
     "families": ["sbert"], "params": "111M", "note": "日本語 SentenceBERT 系", "extra_pip": ["fugashi", "unidic-lite"]},
    {"id": "sonoisa/sentence-bert-base-ja-mean-tokens-v2", "name": "Sentence-BERT 日本語 v2（sonoisa）", "lang": "ja",
     "families": ["sbert"], "params": "111M", "note": "日本語 SentenceBERT の定番", "extra_pip": ["fugashi", "ipadic"]},
    {"id": "pkshatech/GLuCoSE-base-ja-v2", "name": "GLuCoSE base 日本語 v2（PKSHA）", "lang": "ja",
     "families": ["sbert"], "params": "133M", "note": "日本語文埋め込み。sentencepiece 必要", "extra_pip": ["sentencepiece"]},
    # ---- 多言語
    {"id": "intfloat/multilingual-e5-small", "name": "multilingual-e5-small", "lang": "multi",
     "families": ["sbert", "hf"], "params": "118M", "note": "多言語文埋め込み（軽量・日本語可）。追加ライブラリ不要",
     "recommended": True},
    {"id": "intfloat/multilingual-e5-base", "name": "multilingual-e5-base", "lang": "multi",
     "families": ["sbert", "hf"], "params": "278M", "note": "多言語文埋め込み（標準）"},
    {"id": "intfloat/multilingual-e5-large", "name": "multilingual-e5-large", "lang": "multi",
     "families": ["sbert", "hf"], "params": "560M", "note": "多言語文埋め込み（高精度・重い）"},
    {"id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "name": "paraphrase-multilingual-MiniLM-L12-v2",
     "lang": "multi", "families": ["sbert", "hf"], "params": "118M", "note": "軽量な多言語 SentenceBERT", "recommended": True},
    {"id": "sentence-transformers/paraphrase-multilingual-mpnet-base-v2", "name": "paraphrase-multilingual-mpnet-base-v2",
     "lang": "multi", "families": ["sbert", "hf"], "params": "278M", "note": "多言語 SentenceBERT（高精度）"},
    {"id": "BAAI/bge-m3", "name": "BGE-M3", "lang": "multi", "families": ["sbert"], "params": "568M",
     "note": "多言語・長文対応の埋め込み（重い）"},
    {"id": "FacebookAI/xlm-roberta-base", "name": "XLM-RoBERTa base", "lang": "multi",
     "families": ["hf"], "params": "278M", "note": "多言語のファインチューニング定番"},
    {"id": "google-bert/bert-base-multilingual-cased", "name": "BERT base multilingual cased", "lang": "multi",
     "families": ["hf"], "params": "178M", "note": "多言語 BERT"},
    {"id": "microsoft/mdeberta-v3-base", "name": "mDeBERTa-v3 base", "lang": "multi",
     "families": ["hf"], "params": "278M", "note": "多言語 DeBERTa（高精度）。sentencepiece 必要", "extra_pip": ["sentencepiece"]},
    # ---- 英語
    {"id": "google-bert/bert-base-uncased", "name": "BERT base uncased", "lang": "en",
     "families": ["hf", "sbert"], "params": "110M", "note": "英語 BERT の定番", "recommended": True},
    {"id": "distilbert/distilbert-base-uncased", "name": "DistilBERT base uncased", "lang": "en",
     "families": ["hf", "sbert"], "params": "66M", "note": "BERT の蒸留版（速い）"},
    {"id": "FacebookAI/roberta-base", "name": "RoBERTa base", "lang": "en",
     "families": ["hf", "sbert"], "params": "125M", "note": "英語の高精度モデル"},
    {"id": "microsoft/deberta-v3-base", "name": "DeBERTa-v3 base", "lang": "en",
     "families": ["hf"], "params": "184M", "note": "英語 SOTA 級。sentencepiece 必要", "extra_pip": ["sentencepiece"]},
    {"id": "answerdotai/ModernBERT-base", "name": "ModernBERT base", "lang": "en",
     "families": ["hf"], "params": "149M", "note": "長文対応の新世代 BERT。transformers>=4.48"},
    {"id": "sentence-transformers/all-MiniLM-L6-v2", "name": "all-MiniLM-L6-v2", "lang": "en",
     "families": ["sbert", "hf"], "params": "22M", "note": "最軽量の英語 SentenceBERT", "recommended": True},
    {"id": "sentence-transformers/all-mpnet-base-v2", "name": "all-mpnet-base-v2", "lang": "en",
     "families": ["sbert", "hf"], "params": "110M", "note": "英語 SentenceBERT の高精度版"},
    {"id": "BAAI/bge-small-en-v1.5", "name": "BGE small en v1.5", "lang": "en",
     "families": ["sbert", "hf"], "params": "33M", "note": "軽量な英語埋め込み"},
    {"id": "prajjwal1/bert-tiny", "name": "BERT tiny（動作確認用）", "lang": "en",
     "families": ["hf", "sbert"], "params": "4M", "note": "極小。パイプラインの動作確認向け（精度は期待しない）"},
]

# ハイパーパラメータのスキーマ。type: int / float / bool / select / text
_COMMON_TRAIN = [
    {"key": "epochs", "label": "エポック数", "type": "int", "default": 3, "min": 1, "max": 200, "group": "basic",
     "help": "学習データを何周するか"},
    {"key": "batch_size", "label": "バッチサイズ", "type": "int", "default": 16, "min": 1, "max": 1024, "group": "basic"},
    {"key": "lr", "label": "学習率", "type": "float", "default": 3e-5, "min": 1e-7, "max": 1.0, "step": "any",
     "group": "basic"},
    {"key": "weight_decay", "label": "Weight decay", "type": "float", "default": 0.01, "min": 0, "max": 1,
     "step": "any", "group": "advanced"},
    {"key": "warmup_ratio", "label": "ウォームアップ比率", "type": "float", "default": 0.1, "min": 0, "max": 0.5,
     "step": "any", "group": "advanced", "help": "学習率を線形に立ち上げるステップの割合"},
    {"key": "grad_clip", "label": "勾配クリップ", "type": "float", "default": 1.0, "min": 0, "max": 100,
     "step": "any", "group": "advanced", "help": "0 で無効"},
    {"key": "early_stopping", "label": "早期終了（我慢エポック数）", "type": "int", "default": 3, "min": 0, "max": 100,
     "group": "advanced", "help": "検証指標が改善しないエポックがこの数続いたら停止。0 で無効"},
    {"key": "class_weight", "label": "クラス重み（不均衡対策）", "type": "bool", "default": False, "group": "advanced",
     "task": "classification", "help": "少数クラスの損失を逆頻度で重み付け"},
    {"key": "loss", "label": "回帰の損失関数", "type": "select", "default": "mse",
     "options": [{"value": "mse", "label": "MSE（二乗誤差）"}, {"value": "huber", "label": "Huber（外れ値に頑健）"}],
     "group": "advanced", "task": "regression"},
]


def _with(defaults: dict, extra: list[dict] | None = None, drop: tuple = ()) -> list[dict]:
    out = []
    for f in _COMMON_TRAIN:
        if f["key"] in drop:
            continue
        f = dict(f)
        if f["key"] in defaults:
            f["default"] = defaults[f["key"]]
        out.append(f)
    return out + list(extra or [])


HPARAMS = {
    "hf": _with({"epochs": 3, "batch_size": 16, "lr": 3e-5, "early_stopping": 2}, [
        {"key": "max_len", "label": "最大トークン長", "type": "int", "default": 128, "min": 8, "max": 4096,
         "group": "basic", "help": "長い文は切り詰める。大きいほど遅くメモリを使う"},
        {"key": "pooling", "label": "プーリング", "type": "select", "default": "cls",
         "options": [{"value": "cls", "label": "[CLS] トークン"}, {"value": "mean", "label": "平均プーリング"}],
         "group": "advanced"},
        {"key": "freeze_encoder", "label": "エンコーダを凍結（線形プローブ）", "type": "bool", "default": False,
         "group": "advanced", "help": "ON にするとヘッドのみ学習。速いが精度は下がる"},
        {"key": "dropout", "label": "ヘッドの Dropout", "type": "float", "default": 0.1, "min": 0, "max": 0.9,
         "step": "any", "group": "advanced"},
        {"key": "head_hidden", "label": "ヘッド隠れ層", "type": "text", "default": "256",
         "group": "advanced", "help": "カンマ区切り。空なら線形ヘッド"},
        {"key": "fp16", "label": "混合精度（CUDA のみ）", "type": "bool", "default": True, "group": "advanced"},
    ]),
    "sbert": _with({"epochs": 40, "batch_size": 64, "lr": 1e-3, "weight_decay": 1e-4, "warmup_ratio": 0.05,
                    "early_stopping": 6}, [
        {"key": "max_len", "label": "最大トークン長", "type": "int", "default": 256, "min": 8, "max": 4096,
         "group": "basic"},
        {"key": "head_hidden", "label": "MLP 隠れ層", "type": "text", "default": "256,64",
         "group": "basic", "help": "カンマ区切り（例: 256,64）。空なら線形"},
        {"key": "dropout", "label": "Dropout", "type": "float", "default": 0.1, "min": 0, "max": 0.9,
         "step": "any", "group": "advanced"},
        {"key": "normalize", "label": "埋め込みを L2 正規化", "type": "bool", "default": True, "group": "advanced"},
        {"key": "embed_batch", "label": "埋め込みバッチサイズ", "type": "int", "default": 32, "min": 1, "max": 512,
         "group": "advanced"},
    ]),
    "scratch": _with({"epochs": 20, "batch_size": 32, "lr": 5e-4, "early_stopping": 4}, [
        {"key": "tokenizer", "label": "トークナイザ", "type": "select", "default": "auto",
         "options": [{"value": "auto", "label": "自動（日本語→文字 / 英語→単語）"},
                     {"value": "char", "label": "文字単位"}, {"value": "word", "label": "単語単位（空白区切り）"}],
         "group": "basic"},
        {"key": "max_len", "label": "最大トークン長", "type": "int", "default": 128, "min": 8, "max": 2048,
         "group": "basic"},
        {"key": "vocab_size", "label": "語彙サイズ上限", "type": "int", "default": 8000, "min": 50, "max": 100000,
         "group": "advanced"},
        {"key": "d_model", "label": "埋め込み次元 d_model", "type": "int", "default": 128, "min": 8, "max": 1024,
         "group": "basic"},
        {"key": "nhead", "label": "アテンションヘッド数", "type": "int", "default": 4, "min": 1, "max": 32,
         "group": "basic", "help": "d_model を割り切れる数"},
        {"key": "layers", "label": "エンコーダ層数", "type": "int", "default": 2, "min": 1, "max": 24, "group": "basic"},
        {"key": "ff_dim", "label": "FFN 次元", "type": "int", "default": 256, "min": 8, "max": 8192, "group": "advanced"},
        {"key": "dropout", "label": "Dropout", "type": "float", "default": 0.1, "min": 0, "max": 0.9,
         "step": "any", "group": "advanced"},
        {"key": "pooling", "label": "プーリング", "type": "select", "default": "cls",
         "options": [{"value": "cls", "label": "[CLS] トークン"}, {"value": "mean", "label": "平均プーリング"}],
         "group": "advanced"},
    ]),
    "tabular": _with({"epochs": 60, "batch_size": 64, "lr": 1e-3, "weight_decay": 1e-4, "warmup_ratio": 0.05,
                      "early_stopping": 8}, [
        {"key": "d_token", "label": "トークン次元 d_token", "type": "int", "default": 64, "min": 8, "max": 512,
         "group": "basic"},
        {"key": "nhead", "label": "アテンションヘッド数", "type": "int", "default": 4, "min": 1, "max": 32,
         "group": "basic"},
        {"key": "layers", "label": "エンコーダ層数", "type": "int", "default": 2, "min": 1, "max": 12, "group": "basic"},
        {"key": "dropout", "label": "Dropout", "type": "float", "default": 0.1, "min": 0, "max": 0.9,
         "step": "any", "group": "advanced"},
    ]),
    "baseline": [],
}


def family(fid: str) -> dict | None:
    for f in FAMILIES:
        if f["id"] == fid:
            return f
    return None


def preset(model_id: str) -> dict | None:
    for p in PRESETS:
        if p["id"] == model_id:
            return p
    return None


def default_hparams(fid: str, task: str) -> dict:
    out = {}
    for f in HPARAMS.get(fid, []):
        if f.get("task") and f["task"] != task:
            continue
        out[f["key"]] = f["default"]
    return out


def coerce_hparams(fid: str, task: str, given: dict | None) -> dict:
    """UI から来た値を型変換し、範囲に収める。未指定は既定値。"""
    given = given or {}
    out = {}
    for f in HPARAMS.get(fid, []):
        if f.get("task") and f["task"] != task:
            continue
        key = f["key"]
        raw = given.get(key, f["default"])
        try:
            if f["type"] == "int":
                v = int(float(raw))
            elif f["type"] == "float":
                v = float(raw)
            elif f["type"] == "bool":
                v = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "on", "yes")
            elif f["type"] == "select":
                v = str(raw)
                if v not in {o["value"] for o in f["options"]}:
                    v = f["default"]
            else:
                v = str(raw)
        except (TypeError, ValueError):
            v = f["default"]
        if f["type"] in ("int", "float"):
            if "min" in f:
                v = max(f["min"], v)
            if "max" in f:
                v = min(f["max"], v)
        out[key] = v
    return out


def catalog_payload() -> dict:
    return {"families": FAMILIES, "presets": PRESETS, "hparams": HPARAMS}
