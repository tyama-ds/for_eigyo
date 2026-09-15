"""Deterministic, local bibliometrics with explicit retrospective limitations.

No citation edges or annual citation counts are inferred from cumulative totals.
The numerical forecasts use publication counts only. LLM interpretation is a
separate, optional service and cannot replace these measured quantities.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from functools import lru_cache
import hashlib
from itertools import combinations
import math
import re
import warnings as python_warnings

import numpy as np
from scipy import sparse
from sklearn.cluster import KMeans, MiniBatchKMeans
from sklearn.decomposition import PCA, TruncatedSVD
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS, TfidfTransformer, TfidfVectorizer
from sklearn.manifold import TSNE
from sklearn.neighbors import NearestNeighbors

from .frontiers import analyze_frontiers
from .dates import normalize_paper_date
from .embedding_models import LEGACY_TRANSFORMER_MODEL, resolve_embedding_model
from .topic_models import LARGE_CORPUS_THRESHOLD, LEXICAL_BATCH_SIZE, SEMANTIC_PAPER_LIMIT, fit_topic_model
from .cluster_models import CLUSTER_MODELS, fit_cluster_model
from .limits import MAX_ANALYSIS_YEARS, MAX_DATASET_PAPERS
from .field_analysis import store_nmf_geometry
from .author_network import build_author_network
from .text_metadata import analysis_abstract, is_test_summary


TRANSFORMER_MODEL = LEGACY_TRANSFORMER_MODEL
MAP_LIMIT = 400
AUTHOR_LIMIT = 120
EDGE_LIMIT = 240
TRANSFORMER_CHARACTER_LIMIT = 32000
TRANSFORMER_WINDOWS_PER_DOCUMENT = 12
TRANSFORMER_WINDOW_BUDGET = 40000
COLORS = ["#42e8cf", "#9d8cff", "#5fa8ff", "#ffbc6b", "#ff7ea8", "#b5e875",
          "#58d7ff", "#dc93f5", "#f1dd72", "#90b9a5"]
STOP_WORDS = sorted(set(ENGLISH_STOP_WORDS) | {
    "abstract", "paper", "papers", "study", "studies", "research", "results",
    "result", "proposed", "propose", "using", "used", "use", "based", "method",
    "methods", "approach", "show", "shown", "new", "also", "however", "et", "al",
    "copyright", "elsevier", "rights", "reserved",
})


class TransformerError(RuntimeError):
    """A user-readable error that must never cause an implicit TF-IDF fallback."""


def _integer(value, default, low, high, name):
    if value is None:
        return default
    try:
        result = int(value)
        if isinstance(value, bool) or float(value) != result or not low <= result <= high:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name}は {low}〜{high} の整数で指定してください。") from None
    return result


def _count(value):
    try:
        numeric = float(value)
        if math.isfinite(numeric) and 0 <= numeric <= 2 ** 53 - 1 and numeric.is_integer():
            return int(numeric)
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _normalize_papers(papers, first_year, last_year, notices):
    filtered = []
    skipped = 0
    invalid_history = 0
    date_notices = Counter()
    seen = set()
    for index, raw in enumerate(papers):
        year = _count(raw.get("year"))
        if year is None:
            skipped += 1
            continue
        if not first_year <= year <= last_year:
            continue
        identifier = str(raw.get("id") or f"paper-{index + 1}")
        if identifier in seen:
            raise ValueError("論文 ID が重複しています。取り込み時に重複を除去してください。")
        seen.add(identifier)
        authors = []
        author_ids = set()
        for author in raw.get("authors") or []:
            if isinstance(author, str):
                author = {"name": author}
            name = str(author.get("name") or author.get("id") or "").strip()
            aid = str(author.get("id") or f"name:{name.casefold()}")
            if name and aid not in author_ids:
                normalized_author = deepcopy(author)
                normalized_author.update(id=aid, name=name)
                authors.append(normalized_author)
                author_ids.add(aid)
        keywords = sorted({re.sub(r"\s+", " ", str(term)).strip().casefold()
                           for term in (raw.get("keywords") or []) if str(term).strip()})
        history = {}
        for history_year, value in (raw.get("citation_history") or {}).items():
            hy = _count(history_year)
            hv = _count(value)
            if hy is not None and year <= hy <= date.today().year and hv is not None:
                history[str(hy)] = hv
            else:
                invalid_history += 1
        normalized = {"id": identifier, "title": str(raw.get("title") or ""),
                      "abstract": str(raw.get("abstract") or ""), "year": year,
                      "authors": authors, "keywords": keywords,
                      "citations": _count(raw.get("citations")),
                      "doi": str(raw.get("doi") or ""),
                      "source": str(raw.get("source") or ""),
                      "citation_history": history}
        for field in ("providers", "external_url", "citation_source", "citation_snapshots",
                      "citation_history_snapshots", "retrieved_at", "aliases", "provenance", "provenances",
                      "affiliations", "references", "references_status"):
            if field in raw:
                normalized[field] = deepcopy(raw[field])
        publication_date = normalize_paper_date(raw)
        date_notices.update(publication_date.pop("warnings", []))
        normalized.update(publication_date)
        filtered.append(normalized)
    if skipped:
        notices.append(f"出版年が不明な {skipped} 件を分析対象から除外しました。")
    if invalid_history:
        notices.append(f"出版前・未来年・不正値の引用履歴 {invalid_history} 個を除外しました。")
    notices.extend(f"{count} 件: {message}" for message, count in date_notices.items())
    # A stable input order makes labels, sampling and maps reproducible after CSV reordering.
    return sorted(filtered, key=lambda item: item["id"])


def _tfidf(papers, notices, progress_callback=None):
    documents = [" ".join([p["title"], analysis_abstract(p["abstract"]), " ".join(p["keywords"])]) for p in papers]
    test_count = sum(is_test_summary(p["abstract"]) for p in papers)
    if test_count:
        notices.append(f"{test_count} 件のタイトル由来テスト要約は、既知の TEST SUMMARY 接頭辞を除いて文章分析します。元の抄録は保持しています。実抄録を使った分析ではありません。")
    vectorizer = TfidfVectorizer(stop_words=STOP_WORDS, ngram_range=(1, 2),
                                max_features=6000, sublinear_tf=True, strip_accents="unicode",
                                token_pattern=r"(?u)\b[^\W\d_][\w-]+\b", dtype=np.float32)
    try:
        if len(papers) > LARGE_CORPUS_THRESHOLD:
            # Count vocabulary in a streaming pass before building a sparse
            # document matrix. max_features alone prunes only after sklearn has
            # materialized the potentially enormous unpruned matrix.
            frequencies = Counter()
            analyzer = vectorizer.build_analyzer()
            for index, document in enumerate(documents):
                frequencies.update(analyzer(document))
                if progress_callback and (index + 1) % LEXICAL_BATCH_SIZE == 0:
                    progress_callback(f"全件の語彙を集計 {index + 1:,}/{len(documents):,} 件")
            chosen = sorted(frequencies, key=lambda term: (-frequencies[term], term))[:6000]
            del frequencies
            if not chosen:
                raise ValueError("empty vocabulary")
            terms = np.asarray(sorted(chosen))
            counter = CountVectorizer(stop_words=STOP_WORDS, ngram_range=(1, 2),
                strip_accents="unicode", token_pattern=vectorizer.token_pattern,
                vocabulary={str(term): index for index, term in enumerate(terms)}, dtype=np.float32)
            chunks = []
            for start in range(0, len(documents), LEXICAL_BATCH_SIZE):
                chunks.append(counter.transform(documents[start:start + LEXICAL_BATCH_SIZE]))
                if progress_callback:
                    progress_callback(f"全件の疎行列を作成 {min(start + LEXICAL_BATCH_SIZE, len(documents)):,}/{len(documents):,} 件")
            matrix = sparse.vstack(chunks, format="csr")
            del chunks
            matrix = TfidfTransformer(sublinear_tf=True).fit_transform(matrix)
            notices.append("大規模 TF-IDF は全論文の語出現数から最大 6,000 特徴を選び、分割した疎行列で計算しました。"
                           "IDF・分類・年別集計には全対象論文を使用しています。")
        else:
            matrix = vectorizer.fit_transform(documents)
            terms = vectorizer.get_feature_names_out()
    except ValueError as error:
        if "empty vocabulary" not in str(error):
            raise
        matrix = sparse.csr_matrix(np.ones((len(papers), 1), dtype=np.float32))
        terms = np.array(["情報不足"])
        notices.append("有効な語彙を抽出できませんでした。単一トピックとして扱います。")
    empty_rows = np.asarray(matrix.getnnz(axis=1) == 0).ravel()
    if empty_rows.any():
        # Keep all papers while marking no-information papers in a distinct, real dimension.
        matrix = sparse.hstack([matrix, sparse.csr_matrix(empty_rows.astype(np.float32)[:, None])],
                               format="csr")
        terms = np.append(terms, "情報不足")
        notices.append(f"有効な語彙がない {int(empty_rows.sum())} 件は情報不足として扱います。")
    matrix.sort_indices()
    return documents, matrix, terms


@lru_cache(maxsize=1)
def _load_transformer(model_name):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise TransformerError("Transformer を利用するには requirements-transformer.txt の追加依存をインストールしてください。") from None
    except (OSError, RuntimeError) as error:
        raise TransformerError("Transformer の実行環境を読み込めませんでした。PyTorch と OS の実行ライブラリの互換性を確認してください。Windows の DLL 読み込みエラーの場合は、対応する PyTorch と Visual C++ ランタイムが必要です。") from error
    from .storage import data_root
    return SentenceTransformer(model_name, device="cpu", cache_folder=str(data_root() / "models"))


def _windowed_embeddings(model, documents):
    """Encode bounded token windows and combine their token-weighted directions.

    Windows do not overlap, so tokens are not double-counted. Overlong papers
    sample windows across their text, retaining both the beginning and the end.
    The tokenizer is used only to define boundaries; SentenceTransformer retains
    its standard pooling/normalization behavior for each individual window.
    """
    tokenizer = model.tokenizer
    maximum = int(model.max_seq_length)
    special_tokens = int(tokenizer.num_special_tokens_to_add(pair=False))
    window_size = min(510, maximum - special_tokens)
    if window_size < 1:
        raise TransformerError("Transformer の最大長が特殊トークン数以下のため、本文の区間を作成できません。")
    windows_by_document = []
    split_documents = 0
    character_capped = set()
    window_capped = set()
    available_windows = 0
    for index, document in enumerate(documents):
        if len(document) > TRANSFORMER_CHARACTER_LIMIT:
            half = (TRANSFORMER_CHARACTER_LIMIT - 1) // 2
            document = document[:half] + " " + document[-(TRANSFORMER_CHARACTER_LIMIT - half - 1):]
            character_capped.add(index)
        token_ids = tokenizer.encode(document, add_special_tokens=False, truncation=False, verbose=False)
        count = max(1, math.ceil(len(token_ids) / window_size))
        available_windows += count
        split_documents += int(count > 1)
        selected = list(range(count))
        if count > TRANSFORMER_WINDOWS_PER_DOCUMENT:
            selected = np.linspace(0, count - 1, TRANSFORMER_WINDOWS_PER_DOCUMENT, dtype=int).tolist()
            window_capped.add(index)
        document_windows = []
        for number in selected:
            ids = token_ids[number * window_size:(number + 1) * window_size]
            decoded = tokenizer.decode(ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            document_windows.append((decoded, max(1, len(ids))))
        windows_by_document.append(document_windows)
    requested = sum(len(windows) for windows in windows_by_document)
    if requested > TRANSFORMER_WINDOW_BUDGET:
        # Equal initial allocation prevents early papers from consuming the entire budget.
        per_document = max(1, TRANSFORMER_WINDOW_BUDGET // len(documents))
        quotas = [min(len(windows), per_document) for windows in windows_by_document]
        remaining = max(0, TRANSFORMER_WINDOW_BUDGET - sum(quotas))
        while remaining:
            changed = False
            for index, windows in enumerate(windows_by_document):
                if quotas[index] < len(windows):
                    quotas[index] += 1
                    remaining -= 1
                    changed = True
                if remaining == 0:
                    break
            if not changed:
                break
        for index, windows in enumerate(windows_by_document):
            if quotas[index] < len(windows):
                # Quotas >= 2 preserve both ends; a 1-window quota samples the middle.
                selected = (np.linspace(0, len(windows) - 1, quotas[index], dtype=int).tolist()
                            if quotas[index] > 1 else [len(windows) // 2])
                windows_by_document[index] = [windows[item] for item in selected]
                window_capped.add(index)
    chunks = [(index, text, weight) for index, windows in enumerate(windows_by_document)
              for text, weight in windows]
    weighted = None
    total_weights = np.zeros(len(documents), dtype=float)
    for start in range(0, len(chunks), 512):
        batch = chunks[start:start + 512]
        embeddings = np.asarray(model.encode([text for _, text, _ in batch], batch_size=32,
                                             show_progress_bar=False, normalize_embeddings=True,
                                             convert_to_numpy=True), dtype=float)
        if embeddings.ndim != 2 or len(embeddings) != len(batch) or not np.isfinite(embeddings).all():
            raise TransformerError("Transformer が有効な文書埋め込みを返しませんでした。")
        if weighted is None:
            weighted = np.zeros((len(documents), embeddings.shape[1]), dtype=float)
        owners = np.array([index for index, _, _ in batch])
        weights = np.array([weight for _, _, weight in batch], dtype=float)
        np.add.at(weighted, owners, embeddings * weights[:, None])
        np.add.at(total_weights, owners, weights)
    weighted /= np.maximum(total_weights, 1)[:, None]
    weighted /= np.maximum(np.linalg.norm(weighted, axis=1), 1e-12)[:, None]
    details = {"strategy": "token_window_weighted_mean", "content_tokens_per_window": window_size,
               "maximum_windows_per_document": TRANSFORMER_WINDOWS_PER_DOCUMENT,
               "maximum_input_characters": TRANSFORMER_CHARACTER_LIMIT,
               "total_window_budget": TRANSFORMER_WINDOW_BUDGET,
               "available_windows": available_windows, "encoded_windows": len(chunks),
               "split_documents": split_documents, "character_capped_documents": len(character_capped),
               "window_capped_documents": len(window_capped),
               "capped_documents": len(character_capped | window_capped)}
    return weighted, details


def _represent(documents, tfidf, embedding, notices=None, details=None, sbert_model=None):
    selection = resolve_embedding_model(embedding, sbert_model)
    if embedding == "tfidf":
        if details is not None:
            details.update(strategy="whole_document_tfidf", **selection)
        return tfidf, selection["model_id"]
    if len(documents) > SEMANTIC_PAPER_LIMIT:
        raise TransformerError(f"SBERT・Transformer のローカル分析上限は {SEMANTIC_PAPER_LIMIT:,} 件です。"
                               "対象期間を絞るか、全件を分析する NMF・LDA・TF-IDF + KMeans を選択してください。")
    model_name = selection["model_id"]
    try:
        model = _load_transformer(model_name)
        matrix, window_details = _windowed_embeddings(model, documents)
    except TransformerError:
        raise
    except Exception as error:
        raise TransformerError("Transformer モデルを読み込み・実行できませんでした。初回ダウンロードのネットワーク接続、追加依存、PyTorch の実行環境を確認してください。") from error
    if not np.isfinite(matrix).all():
        raise TransformerError("Transformer が有限値ではない埋め込みを返しました。")
    if details is not None:
        details.update(window_details, **selection)
    if notices is not None and embedding == "sbert" and selection["preset"] == "mpnet":
        notices.append("MPNet (all-mpnet-base-v2) は英語向けの文埋め込みモデルです。日本語を含む資料では多言語 MiniLM を選択してください。")
    if notices is not None and window_details["capped_documents"]:
        notices.append(f"Transformer 入力の上限により {window_details['capped_documents']} 件の文書を部分抽出しました。"
                       f"文字上限 {TRANSFORMER_CHARACTER_LIMIT:,} 字、1 文書最大 {TRANSFORMER_WINDOWS_PER_DOCUMENT} 区間、"
                       f"全体最大 {TRANSFORMER_WINDOW_BUDGET:,} 区間です。区間処理数 {window_details['encoded_windows']:,}。")
    return matrix, model_name


def _cluster(matrix, requested, notices, progress_callback=None):
    if sparse.issparse(matrix):
        # Sparse fingerprints avoid dense conversion and protect identical/tiny corpora.
        fingerprints = set()
        for i in range(matrix.shape[0]):
            start, end = matrix.indptr[i:i + 2]
            digest = hashlib.blake2b(digest_size=16)
            digest.update(matrix.indices[start:end].tobytes())
            digest.update(np.round(matrix.data[start:end], 10).tobytes())
            fingerprints.add(digest.digest())
            if len(fingerprints) >= requested:
                break
        unique = len(fingerprints)
    else:
        unique = len(np.unique(np.round(matrix, 8), axis=0))
    effective = min(requested, matrix.shape[0], unique)
    if effective < requested:
        notices.append(f"文書数・異なる文書表現数に合わせ、トピック数を {effective} に調整しました。")
    if effective <= 1:
        return np.zeros(matrix.shape[0], dtype=int)
    if matrix.shape[0] > 1500:
        estimator = MiniBatchKMeans(n_clusters=effective, init="random", random_state=42, n_init=5,
                                    batch_size=LEXICAL_BATCH_SIZE if matrix.shape[0] > LARGE_CORPUS_THRESHOLD else 512,
                                    max_iter=100, reassignment_ratio=0)
    else:
        estimator = KMeans(n_clusters=effective, init="random", random_state=42, n_init=10, max_iter=200)
    with python_warnings.catch_warnings():
        python_warnings.simplefilter("ignore", ConvergenceWarning)
        if matrix.shape[0] > LARGE_CORPUS_THRESHOLD:
            rng = np.random.default_rng(42)
            for epoch in range(2):
                order = rng.permutation(matrix.shape[0])
                for start in range(0, matrix.shape[0], LEXICAL_BATCH_SIZE):
                    estimator.partial_fit(matrix[order[start:start + LEXICAL_BATCH_SIZE]])
                    if progress_callback:
                        progress_callback(f"KMeans 学習 {epoch + 1}/2 周・"
                                          f"{min(start + LEXICAL_BATCH_SIZE, matrix.shape[0]):,}/{matrix.shape[0]:,} 件")
            labels = np.empty(matrix.shape[0], dtype=np.int32)
            for start in range(0, matrix.shape[0], LEXICAL_BATCH_SIZE):
                stop = min(start + LEXICAL_BATCH_SIZE, matrix.shape[0])
                labels[start:stop] = estimator.predict(matrix[start:stop])
            notices.append(f"MiniBatchKMeans で全 {matrix.shape[0]:,} 件を 2 周の分割学習・全件分類に使用しました。"
                           "一括 KMeans とは最適化手順が異なります。")
        else:
            labels = estimator.fit_predict(matrix)
    # Remap arbitrary estimator labels by size, then first document index.
    ordered = sorted(np.unique(labels), key=lambda k: (-int((labels == k).sum()),
                                                      int(np.flatnonzero(labels == k)[0])))
    remap = {int(label): i for i, label in enumerate(ordered)}
    if len(ordered) < effective:
        notices.append("クラスタが重なったため、実際に分離できたトピックだけを表示します。")
    return np.array([remap[int(label)] for label in labels])


def _normalize_positions(values):
    values = np.nan_to_num(np.asarray(values, dtype=float))
    positions = np.full((len(values), 2), 0.5, dtype=float)
    for axis in range(min(values.shape[1], 2)):
        column = values[:, axis]
        # Fix projection sign to make the biggest absolute coordinate positive.
        if column.size and column[np.argmax(np.abs(column))] < 0:
            column = -column
        spread = float(np.ptp(column))
        if spread > 1e-10:
            positions[:, axis] = 0.05 + 0.9 * (column - float(column.min())) / spread
    return positions


def _project(matrix):
    """Linear fallback for tiny or degenerate display samples only."""
    if matrix.shape[0] <= 1 or matrix.shape[1] <= 1:
        return np.full((matrix.shape[0], 2), 0.5)
    if sparse.issparse(matrix):
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("ignore", RuntimeWarning)
            reduced = TruncatedSVD(n_components=2, random_state=42).fit_transform(matrix)
    else:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("ignore", RuntimeWarning)
            reduced = PCA(n_components=2, svd_solver="randomized", random_state=42).fit_transform(matrix)
    return _normalize_positions(reduced)


def _projection_inputs(matrix):
    """Bounded, reusable display representation; never fits a time slice."""
    sample_count, dimensions = matrix.shape
    if not sample_count:
        return np.zeros((0, min(50, dimensions))), "empty"
    reduced_dimensions = min(50, dimensions - 1, max(1, sample_count - 1))
    if dimensions > 50 and reduced_dimensions >= 1:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("ignore", RuntimeWarning)
            reduced = TruncatedSVD(n_components=reduced_dimensions, random_state=42).fit_transform(matrix)
        reduction = f"SVD ({reduced_dimensions}D)"
    else:
        reduced = matrix.toarray() if sparse.issparse(matrix) else np.asarray(matrix)
        reduction = "none"
    reduced = np.array(reduced, dtype=float, copy=True)
    reduced /= np.maximum(np.linalg.norm(reduced, axis=1), 1e-12)[:, None]
    return reduced, reduction


def _map_projection(matrix, embedding, requested_method="auto"):
    """Project only the bounded display sample; topic assignment is independent."""
    sample_count, dimensions = matrix.shape
    if requested_method not in {"auto", "tsne", "pca", "umap"}:
        raise ValueError("地図の投影法は auto・tsne・pca・umap を指定してください。")
    prefix = {"tfidf": "TF-IDF", "nmf": "NMF 文書–トピック重み", "lda": "LDA 文書–トピック確率"}.get(embedding, "Sentence Transformer")
    linear = "SVD" if sparse.issparse(matrix) else "PCA"

    def fallback(reason):
        return (_project(matrix), f"{prefix} → {linear} (2D; 少数・重複文書の線形投影)",
                {"algorithm": linear.lower(), "sample_count": sample_count,
                 "input_dimensions": dimensions, "fallback_reason": reason,
                 "requested_method": requested_method, "random_state": 42})

    umap_type = None
    if requested_method == "umap":
        try:
            from umap import UMAP
            umap_type = UMAP
        except ImportError as exc:
            raise ValueError("UMAP が未導入です。requirements.txt の umap-learn をインストールして再起動してください。") from exc
    if requested_method == "pca" and min(sample_count, dimensions) >= 2:
        reduced, reduction = _projection_inputs(matrix)
        if min(reduced.shape) >= 2:
            with python_warnings.catch_warnings():
                python_warnings.simplefilter("ignore", RuntimeWarning)
                estimator = PCA(n_components=2, svd_solver="full")
                projected = estimator.fit_transform(reduced)
            ratios = np.nan_to_num(estimator.explained_variance_ratio_).tolist()
            return (_normalize_positions(projected), f"{prefix} → PCA (2D)",
                    {"algorithm": "pca", "requested_method": requested_method,
                     "sample_count": sample_count, "input_dimensions": dimensions,
                     "reduced_dimensions": reduced.shape[1], "reduction": reduction,
                     "explained_variance_ratio": ratios, "random_state": 42})
    if sample_count < 6 or dimensions < 2:
        return fallback("tiny_sample")
    # Retain up to 50 directions before t-SNE. This is never a 10,000-paper distance matrix.
    reduced, reduction_name = _projection_inputs(matrix)
    reduced_dimensions = reduced.shape[1]
    reduction = "" if reduction_name == "none" else reduction_name + " → "
    if len(np.unique(np.round(reduced, 9), axis=0)) < 3:
        return fallback("insufficient_unique_vectors")
    if requested_method == "umap":
        neighbors = min(15, sample_count - 1)
        estimator = umap_type(n_components=2, n_neighbors=neighbors, min_dist=0.1,
                              metric="cosine", random_state=42, n_jobs=1, init="random")
        projected = estimator.fit_transform(reduced)
        if not np.isfinite(projected).all():
            raise ValueError("UMAP の座標が非有限値になりました。PCA を選択してください。")
        return (_normalize_positions(projected), f"{prefix} → {reduction}UMAP (2D; cosine)",
                {"algorithm": "umap", "requested_method": requested_method,
                 "sample_count": sample_count, "input_dimensions": dimensions,
                 "reduced_dimensions": reduced_dimensions, "n_neighbors": neighbors,
                 "min_dist": 0.1, "metric": "cosine", "random_state": 42})
    perplexity = min(30.0, max(2.0, (sample_count - 1) / 3))
    estimator = TSNE(n_components=2, perplexity=perplexity, metric="cosine", init="pca",
                     learning_rate="auto", random_state=42, max_iter=750,
                     method="barnes_hut", n_jobs=1)
    projected = estimator.fit_transform(reduced)
    if not np.isfinite(projected).all():
        return fallback("nonfinite_tsne_coordinates")
    return (_normalize_positions(projected),
            f"{prefix} → {reduction}t-SNE (2D; cosine; perplexity={perplexity:.1f})",
            {"algorithm": "tsne", "requested_method": requested_method,
             "selection_reason": "usable_sample" if requested_method == "auto" else "user_selected",
             "sample_count": sample_count, "input_dimensions": dimensions,
             "reduced_dimensions": reduced_dimensions, "perplexity": round(perplexity, 3),
             "random_state": 42, "maximum_iterations": 750, "metric": "cosine"})


def _smoothed_growth(counts):
    """Compare equal 1/2-year windows; +1 paper/year stabilizes tiny denominators."""
    if len(counts) < 2:
        return 0.0
    window = min(2, len(counts) // 2)
    recent = float(np.mean(counts[-window:]))
    previous = float(np.mean(counts[-2 * window:-window]))
    return 100.0 * (recent - previous) / (previous + 1.0)


def _count_prediction(counts, horizon):
    """Ridge log-linear extrapolation with a damped trend and explicit growth cap."""
    values = np.asarray(counts, dtype=float)
    if len(values) == 1:
        slope, level = 0.0, float(np.log1p(values[-1]))
        fitted = values.copy()
    else:
        x = np.arange(len(values), dtype=float)
        center = x - x.mean()
        logs = np.log1p(values)
        slope = float(np.dot(center, logs - logs.mean()) / (np.dot(center, center) + 2.0))
        fitted_log = logs.mean() + slope * center
        level = 0.65 * float(fitted_log[-1]) + 0.35 * float(logs[-1])
        fitted = np.expm1(fitted_log).clip(0)
    scale = math.sqrt(float(np.mean((values - fitted) ** 2))) + math.sqrt(float(values[-1]) + 1)
    output = []
    damped_steps = 0.0
    recent_max = float(np.max(values[-3:]))
    for step in range(1, horizon + 1):
        damped_steps += 0.75 ** step
        cap = recent_max * (1 + 0.6 * step) + 2 * step
        value = min(cap, max(0.0, math.expm1(min(30, level + slope * damped_steps))))
        width = 1.5 * scale * math.sqrt(step)
        output.append({"value": value, "lower": max(0.0, value - width),
                       "upper": min(cap, value + width)})
    return output


def _insufficient_history():
    return {"mae": None, "baseline_mae": None, "folds": 0,
            "model": "insufficient_history", "fold_details": []}


def _forecast(counts, last_year, horizon):
    if len(counts) < 2:
        return [], _insufficient_history()
    errors, baseline_errors, details = [], [], []
    for origin in range(3, len(counts)):
        predicted = _count_prediction(counts[:origin], 1)[0]["value"]
        actual, baseline = counts[origin], counts[origin - 1]
        errors.append(abs(actual - predicted))
        baseline_errors.append(abs(actual - baseline))
        details.append({"year": last_year - len(counts) + origin + 1,
                        "actual": int(actual), "prediction": round(predicted, 3),
                        "baseline": int(baseline)})
    mae = float(np.mean(errors)) if errors else None
    baseline_mae = float(np.mean(baseline_errors)) if errors else None
    use_baseline = mae is not None and baseline_mae < mae
    forecasts = _count_prediction(counts, horizon)
    if use_baseline:
        # The same last-observation baseline is deliberately allowed to beat the trend model.
        scale = math.sqrt(float(np.mean(np.diff(counts) ** 2))) + math.sqrt(counts[-1] + 1)
        for step, forecast in enumerate(forecasts, 1):
            value = float(counts[-1])
            cap = float(max(counts[-3:])) * (1 + 0.6 * step) + 2 * step
            width = 1.5 * scale * math.sqrt(step)
            forecast.update(value=value, lower=max(0, value - width), upper=min(cap, value + width))
    for step, forecast in enumerate(forecasts, 1):
        forecast["year"] = last_year + step
        for key in ("value", "lower", "upper"):
            forecast[key] = round(forecast[key], 3)
    return forecasts, {"mae": round(mae, 3) if mae is not None else None,
                       "baseline_mae": round(baseline_mae, 3) if baseline_mae is not None else None,
                       "folds": len(errors), "model": "last_observation" if use_baseline else "damped_log_trend",
                       "fold_details": details}


def _citation_events(papers, years):
    rows = []
    for year in years:
        eligible = [p for p in papers if p["year"] <= year]
        observed = [p["citation_history"][str(year)] for p in eligible if str(year) in p["citation_history"]]
        rows.append({"year": year, "annual_citations": sum(observed) if observed else None,
                     "annual_observed_papers": len(observed), "annual_eligible_papers": len(eligible),
                     "annual_coverage": round(100 * len(observed) / len(eligible), 2) if eligible else None})
    return rows


def _citation_growth(papers, years):
    if len(years) < 2:
        return None
    # Fixed cohort: comparing changing paper coverage would manufacture apparent acceleration.
    cohort = [p for p in papers if p["year"] <= years[-2]]
    if not cohort or not all(str(year) in p["citation_history"] for p in cohort for year in years[-2:]):
        return None
    previous, recent = [sum(p["citation_history"][str(year)] for p in cohort) for year in years[-2:]]
    return 100 * (recent - previous) / (previous + 1)


def _topic_keywords(tfidf, terms, indices):
    weights = np.asarray(tfidf[indices].mean(axis=0)).ravel()
    ranked = np.argsort(-weights, kind="stable")
    chosen = []
    for index in ranked:
        if weights[index] <= 0:
            break
        term = str(terms[index])
        tokens = set(term.split())
        if any(tokens <= set(existing.split()) or set(existing.split()) <= tokens for existing in chosen):
            continue
        chosen.append(term)
        if len(chosen) >= 6:
            break
    return chosen or ["情報不足"]


def _evidence_papers(papers):
    cited = sorted(papers, key=lambda p: (-(p["citations"] or 0), not bool(p["abstract"].strip()),
                                         -p["year"], p["id"]))
    recent = sorted(papers, key=lambda p: (-p["year"], not bool(p["abstract"].strip()),
                                          -(p["citations"] or 0), p["id"]))
    chosen = []
    identifiers = set()
    # Alternate citation and recency evidence, so even a four-item prompt sees both.
    for ranking in (cited, recent, cited, recent, cited):
        item = next((paper for paper in ranking if paper["id"] not in identifiers), None)
        if item is not None:
            chosen.append(item)
            identifiers.add(item["id"])
    return chosen


def _build_topics(papers, labels, tfidf, terms, years, horizon, totals,
                  keyword_override=None, outlier_label=None, information_insufficient_label=None,
                  annual_comparison=True):
    result = []
    window = min(2, max(1, len(years) // 2))
    annual_comparison = annual_comparison and len(years) > 1
    for label in sorted(set(labels)):
        indices = np.flatnonzero(labels == label)
        members = [papers[i] for i in indices]
        known_citations = [p for p in members if p["citations"] is not None]
        citation_total = sum(p["citations"] for p in known_citations) if known_citations else None
        citation_aggregate = {"citation_total": citation_total,
                              "citation_known_count": len(known_citations),
                              "citation_mean": round(citation_total / len(known_citations), 6)
                                               if known_citations else None}
        counts_by_year = Counter(p["year"] for p in members)
        counts = [counts_by_year[year] for year in years]
        keywords = (list(keyword_override[label]) if keyword_override is not None and label in keyword_override
                    else _topic_keywords(tfidf, terms, indices))
        is_outlier = outlier_label is not None and int(label) == int(outlier_label)
        if is_outlier:
            # Keep observations and their IDs; unassigned records are not a technology.
            result.append({"id": f"topic-{label + 1}",
                           "label": "情報不足" if label == information_insufficient_label else "未分類",
                           "keywords": [], "count": len(members),
                           "citations": sum(p["citations"] or 0 for p in members),
                           **citation_aggregate,
                           "growth_pct": None, "share": round(100 * len(members) / len(papers), 2),
                           "score": 0.0, "status": "unclassified", "is_outlier": True,
                           "color": "#8b96aa", "series": [
                               {"year": year, "count": counts[i],
                                "share": round(100 * counts[i] / max(1, totals[i]), 2)}
                               for i, year in enumerate(years)],
                           "forecast": [], "backtest": _insufficient_history(),
                           "annual_citation_series": _citation_events(members, years),
                           "citation_growth_pct": None, "citation_age_proxy": None,
                           "score_components": {"publication_momentum": 0.0, "publication_share": 0.0,
                                                "citation_signal": None, "citation_mode": "unclassified"},
                           "evidence_ids": [p["id"] for p in _evidence_papers(members)],
                           "explanation": "分類条件を満たさない論文です。全体件数と観測値には含めますが、注目スコア・増減判定・予測の対象にはしません。"})
            continue
        growth = _smoothed_growth(counts) if annual_comparison else 0.0
        recent = sum(counts[-window:]) if annual_comparison else len(members)
        recent_total = sum(totals[-window:]) if annual_comparison else len(papers)
        previous = sum(counts[-2 * window:-window]) if annual_comparison else recent
        recent_share = recent / max(1, recent_total)
        previous_share = previous / max(1, sum(totals[-2 * window:-window])) if annual_comparison else recent_share
        momentum = 0.5 + 0.5 * math.tanh(growth / 100)
        score = 100 * (0.6 * momentum + 0.4 * math.sqrt(recent_share))
        citation_growth = _citation_growth(members, years)
        citation_proxy = (sum(p["citations"] / (date.today().year - p["year"] + 1)
                              for p in known_citations) / len(known_citations)) if known_citations else None
        citation_mode = "unavailable"
        citation_signal = None
        if citation_growth is not None:
            citation_mode = "annual_fixed_cohort"
            citation_signal = 50 + 50 * math.tanh(citation_growth / 100)
            score = 0.75 * score + 0.25 * citation_signal
        elif citation_proxy is not None:
            citation_mode = "age_adjusted_snapshot"
            citation_signal = 100 * (1 - math.exp(-citation_proxy / 5))
            score = 0.85 * score + 0.15 * citation_signal
        if annual_comparison and growth >= 25 and recent >= 3 and recent_share > previous_share + 0.005:
            status = "emerging"
        elif growth < -25 and recent_share < previous_share - 0.005:
            status = "declining"
        elif recent >= max(3, recent_total * 0.15) and score >= 45:
            status = "hot"
        else:
            status = "stable"
        forecast, backtest = (_forecast(counts, years[-1], horizon) if annual_comparison
                              else ([], _insufficient_history()))
        evidence = _evidence_papers(members)
        citation_note = (f"同一の既刊論文群の年間引用は前年から {citation_growth:+.1f}%（分母+1）です。"
                         if citation_growth is not None else "比較可能な同一論文群の年別引用履歴が不足し、引用増減は判定していません。")
        if citation_mode == "age_adjusted_snapshot":
            citation_note += f"累積引用÷出版経過年の平均 {citation_proxy:.2f} を注目度の代理値として使います（引用増加率ではありません）。"
        result.append({"id": f"topic-{label + 1}", "label": " · ".join(keywords[:2]),
                       "keywords": keywords, "count": len(members),
                       "citations": sum(p["citations"] or 0 for p in members),
                       **citation_aggregate,
                       "growth_pct": round(growth, 2) if annual_comparison else None,
                       "share": round(100 * len(members) / len(papers), 2),
                       "score": round(min(100, max(0, score)), 2), "status": status, "is_outlier": False,
                       "color": COLORS[label % len(COLORS)],
                       "series": [{"year": year, "count": counts[i],
                                   "share": round(100 * counts[i] / max(1, totals[i]), 2)}
                                  for i, year in enumerate(years)],
                       "forecast": forecast, "backtest": backtest,
                       "annual_citation_series": _citation_events(members, years),
                       "citation_growth_pct": round(citation_growth, 2) if citation_growth is not None else None,
                       "citation_age_proxy": round(citation_proxy, 3) if citation_proxy is not None else None,
                       "score_components": {"publication_momentum": round(100 * momentum, 2),
                                            "publication_share": round(100 * math.sqrt(recent_share), 2),
                                            "citation_signal": round(citation_signal, 2) if citation_signal is not None else None,
                                            "citation_mode": citation_mode},
                       "evidence_ids": [p["id"] for p in evidence],
                       "explanation": (f"直近 {window} 年は {recent} 件、前の {window} 年は {previous} 件。"
                                      f"平滑化増減率は {growth:+.1f}%、直近の論文シェアは {recent_share * 100:.1f}%。"
                                      if annual_comparison else f"収録年 {min(counts_by_year)} 年は {recent} 件、論文シェアは {recent_share * 100:.1f}%。実収録または対象期間が1年だけのため年次増減は比較できず、出版数予測は行いません。スコアの出版成長成分は中立値です。")
                                      + citation_note})
    return result


def _build_keywords(papers, tfidf, terms, years, annual_comparison=True):
    occurrences = defaultdict(list)
    for index, paper in enumerate(papers):
        selected = paper["keywords"]
        if not selected:
            row = tfidf.getrow(index)
            ranked = np.argsort(-row.data, kind="stable")[:5]
            selected = [str(terms[row.indices[item]]) for item in ranked if terms[row.indices[item]] != "情報不足"]
        for term in set(selected):
            occurrences[term].append(paper)
    result = []
    for term, members in sorted(occurrences.items(), key=lambda item: (-len(item[1]), item[0]))[:60]:
        counts = Counter(p["year"] for p in members)
        classified = [p for p in members if not p.get("is_outlier")]
        topic = Counter(p["topic_id"] for p in (classified or members)).most_common(1)[0][0]
        result.append({"term": term, "count": len(members),
                       "growth_pct": round(_smoothed_growth([counts[y] for y in years]), 2) if annual_comparison and len(years) > 1 else None,
                       "series": [{"year": year, "count": counts[year]} for year in years],
                       "topic_id": topic})
    return result, len(occurrences)


def _frontier_analysis(papers, topics, keywords, options):
    """Keep the corpus denominator while suppressing unclassified opportunities."""
    excluded = {topic["id"] for topic in topics if topic.get("is_outlier")}
    result = analyze_frontiers(papers, topics,
                              [row for row in keywords if row["topic_id"] not in excluded], options)
    if not excluded:
        return result
    for row in result["monthly"]["topics"]:
        if row["topic_id"] in excluded:
            row.update(status="insufficient", growth_pct=None, share_change_pp=None, is_outlier=True)
    # Phrase support is counted on every paper. Its computed primary association
    # can still be unclassified even if the upstream keyword association was not.
    result["sparse"]["terms"] = [row for row in result["sparse"]["terms"] if row["topic_id"] not in excluded]
    result["sparse"]["candidates"] = [row for row in result["sparse"]["candidates"]
                                      if row["topic_a"] not in excluded and row["topic_b"] not in excluded]
    warning = "未分類・情報不足の論文は全体件数と出現件数の母数に保持しますが、未分類群の月次増減判定と、未分類群を主な所属とする語の組合せ候補からは除外します。"
    result["monthly"]["warnings"].append(warning)
    result["sparse"]["warnings"].append(warning)
    return result


def _map_indices(papers, labels):
    if len(papers) <= MAP_LIMIT:
        return list(range(len(papers)))
    groups = []
    for label in sorted(set(labels)):
        ordered = sorted(np.flatnonzero(labels == label), key=lambda i: (papers[i]["year"], papers[i]["id"]))
        quota = min(len(ordered), MAP_LIMIT // len(set(labels)))
        groups.append([ordered[i] for i in np.linspace(0, len(ordered) - 1, quota, dtype=int)])
    selected = {int(index) for group in groups for index in group}
    remaining = sorted(set(range(len(papers))) - selected, key=lambda i: (papers[i]["year"], papers[i]["id"]))
    extra = min(MAP_LIMIT - len(selected), len(remaining))
    if extra:
        selected.update(remaining[i] for i in np.linspace(0, len(remaining) - 1, extra, dtype=int))
    return sorted(selected)


def _build_map(papers, labels, matrix, embedding, requested_method="auto"):
    indices = _map_indices(papers, labels)
    subset = matrix[indices]
    positions, projection_method, projection_details = (_map_projection(subset, embedding)
        if requested_method == "auto" else _map_projection(subset, embedding, requested_method))
    vectors, reduction = _projection_inputs(subset)
    nodes = [{"id": papers[i]["id"], "label": papers[i]["title"], "topic_id": papers[i]["topic_id"],
              "year": papers[i]["year"], "citations": papers[i]["citations"] or 0,
              "publication_date": papers[i].get("publication_date", ""),
              "date_precision": papers[i].get("date_precision", "year"),
              "x": round(float(positions[j, 0]), 5), "y": round(float(positions[j, 1]), 5)} for j, i in enumerate(indices)]
    edges = []
    if len(indices) > 1:
        search = NearestNeighbors(n_neighbors=min(4, len(indices)), metric="cosine", algorithm="brute")
        distances, neighbors = search.fit(subset).kneighbors(subset)
        found = {}
        for i, (row_distances, row_neighbors) in enumerate(zip(distances, neighbors)):
            for distance, j in zip(row_distances, row_neighbors):
                if i == j:
                    continue
                similarity = max(0.0, min(1.0, 1 - float(distance)))
                if similarity >= (0.25 if embedding == "tfidf" else 0.45):
                    a, b = sorted((nodes[i]["id"], nodes[j]["id"]))
                    found[(a, b)] = similarity
        edges = [{"source": a, "target": b, "weight": round(weight, 4)}
                 for (a, b), weight in sorted(found.items(), key=lambda item: (-item[1], item[0]))[:700]]
    return {"nodes": nodes, "edges": edges,
            "method": projection_method
                      + " / 表示文書間の cosine 近傍（引用関係ではありません）",
            "projection": projection_details,
            "projection_inputs": {"paper_ids": [node["id"] for node in nodes],
                "vectors": np.round(vectors, 9).tolist(), "embedding": embedding,
                "source": "saved_analysis_representation", "dimensions": vectors.shape[1],
                "original_dimensions": subset.shape[1], "reduction": reduction},
            "truncated": len(papers) > MAP_LIMIT}


def _network_positions(nodes, edges):
    n = len(nodes)
    if n <= 1:
        return np.full((n, 2), 0.5)
    theta = np.arange(n) * (2 * np.pi / n)
    positions = np.column_stack((np.cos(theta), np.sin(theta))) * 0.3
    lookup = {node["id"]: i for i, node in enumerate(nodes)}
    adjacency = np.zeros((n, n))
    for edge in edges:
        a, b = lookup[edge["source"]], lookup[edge["target"]]
        adjacency[a, b] = adjacency[b, a] = math.log1p(edge["weight"])
    scale = math.sqrt(1 / n)
    for iteration in range(90):
        difference = positions[:, None, :] - positions[None, :, :]
        distance = np.maximum(np.linalg.norm(difference, axis=2), 0.01)
        repulsion = scale * scale / distance ** 2
        attraction = adjacency * distance / scale
        force = np.sum(difference * (repulsion - attraction)[:, :, None], axis=1)
        norm = np.maximum(np.linalg.norm(force, axis=1), 1e-8)
        temperature = 0.04 * (1 - iteration / 90)
        positions += force / norm[:, None] * np.minimum(norm, temperature)[:, None]
        positions -= positions.mean(axis=0)
    return _normalize_positions(positions)


def _build_network(papers, group_by="community", topics=None):
    network = build_author_network(papers, group_by=group_by, topics=topics)
    return network, network["stats"]["authors_total"]


def analyze(papers: list[dict], options: dict | None = None, progress_callback=None) -> dict:
    """Analyze an imported corpus, returning only JSON-safe values.

    Defaults intentionally exclude the unfinished current calendar year. The
    imported corpus is the population for every statistic; no global Scopus
    coverage or field-normalized citation benchmark is assumed.
    """
    if not papers:
        raise ValueError("論文がありません。Scopus の CSV を取り込んでください。")
    if MAX_DATASET_PAPERS is not None and len(papers) > MAX_DATASET_PAPERS:
        raise ValueError(f"このローカル版の分析上限は {MAX_DATASET_PAPERS:,} 件です。対象期間や検索条件を絞ってください。")
    options = options or {}
    if options.get("map_projection", "auto") not in {"auto", "tsne", "pca", "umap"}:
        raise ValueError("地図の投影法は auto・tsne・pca・umap を指定してください。")
    current_year = date.today().year
    first_year = _integer(options.get("start_year"), current_year - 5, 1800, current_year, "開始年")
    last_year = _integer(options.get("end_year"), current_year - 1, 1800, current_year, "終了年")
    if first_year > last_year:
        raise ValueError("開始年は終了年以前にしてください。")
    if last_year - first_year >= MAX_ANALYSIS_YEARS:
        raise ValueError(f"分析期間は {MAX_ANALYSIS_YEARS} 年以内にしてください。")
    n_topics = _integer(options.get("n_topics"), 8, 1, 20, "トピック数")
    horizon = _integer(options.get("horizon"), 3, 1, 3, "予測年数")
    embedding = options.get("embedding", "tfidf")
    if embedding not in {"tfidf", "transformer", "sbert"}:
        raise ValueError("文書表現は tfidf・sbert・transformer を指定してください。")
    selection = resolve_embedding_model(embedding, options.get("sbert_model"))
    topic_model = options.get("topic_model", "kmeans")
    if topic_model not in {*CLUSTER_MODELS, "nmf", "lda", "bertopic"}:
        raise ValueError("対応するクラスタリング・トピックモデルを指定してください。")
    if topic_model in {"nmf", "lda"} and embedding != "tfidf":
        raise ValueError("NMF・LDA の入力には TF-IDF を選択してください。SBERT の埋め込みから自動変換しません。")
    if topic_model == "bertopic" and embedding not in {"sbert", "transformer"}:
        raise ValueError("BERTopic には SBERT（または互換 Transformer）の文書表現が必要です。")
    min_topic_size = _integer(options.get("min_topic_size"), 5, 2, 1000, "最小トピック件数")
    notices = []
    if last_year == current_year:
        notices.append("当年は未完了です。件数・増減率・予測は年途中の収録件数の影響を受けます。")
    if progress_callback:
        progress_callback(f"対象期間の論文を確認 {len(papers):,} 件")
    selected = _normalize_papers(papers, first_year, last_year, notices)
    if not selected:
        raise ValueError("指定期間に論文がありません。開始年・終了年を変更してください。")
    if embedding != "tfidf" and len(selected) > SEMANTIC_PAPER_LIMIT:
        raise TransformerError(f"SBERT・Transformer・BERTopic のローカル分析上限は {SEMANTIC_PAPER_LIMIT:,} 件です。"
                               "対象期間を絞るか、全件を分析する NMF・LDA・TF-IDF + KMeans を選択してください。")
    years = list(range(first_year, last_year + 1))
    single_year_corpus = options.get("single_year_corpus") is True
    annual_comparison = len(years) > 1 and not single_year_corpus
    if single_year_corpus:
        notices.append("収集範囲が未宣言で実収録年が1年だけのため、未提供年を年次変化の根拠には使わず、成長率・新興判定・出版数予測を表示しません。年別表の0は取得集合内の0です。")
    if len(years) == 1:
        notices.append("対象期間が 1 年のみのため、年次の増減は比較できず、将来の出版数予測は表示しません。分野・著者・語の関係は分析できます。")
    if len(years) < 4:
        notices.append("4 年未満のため、3 年以上の訓練期間を使う予測検証は実行できません。")
    abstract_coverage = 100 * sum(bool(p["abstract"].strip()) for p in selected) / len(selected)
    if abstract_coverage < 100:
        notices.append(f"抄録の収録率は {abstract_coverage:.1f}% です。抄録欠測時はタイトル・キーワードで分析します。")
    citation_history_coverage = 100 * sum(any(str(year) in p["citation_history"] for year in years)
                                        for p in selected) / len(selected)
    snapshot_coverage = 100 * sum(p["citations"] is not None for p in selected) / len(selected)
    if citation_history_coverage == 0:
        notices.append("年別引用履歴がありません。現時点の累積引用から過去の引用推移は復元できません。")
    else:
        notices.append("年別引用は対象論文の取得済み履歴だけを合計します。年ごとの観測率を確認してください。")
    if snapshot_coverage < 100:
        notices.append(f"累積引用数の収録率は {snapshot_coverage:.1f}% です。表示合計は既知の値のみです。")
    if progress_callback:
        progress_callback(f"全 {len(selected):,} 件の文書表現を作成")
    documents, tfidf, terms = _tfidf(selected, notices, progress_callback)
    embedding_details = {}
    matrix, embedding_model = _represent(documents, tfidf, embedding, notices, embedding_details,
                                          sbert_model=options.get("sbert_model"))
    embedding_details.update(corpus_papers=len(selected), matrix_dtype=str(tfidf.dtype),
        vocabulary_scope="full_corpus", vocabulary_limit=6000,
        matrix_storage="sparse_csr" if sparse.issparse(matrix) else "dense_embeddings")
    if progress_callback:
        progress_callback(f"{topic_model.upper()} で全 {len(selected):,} 件を分類")
    if topic_model in CLUSTER_MODELS:
        clustering_matrix = (sparse.csr_matrix(matrix.shape, dtype=np.float32)
            if embedding == "tfidf" and all(term == "情報不足" for term in terms) else matrix)
        fitted = fit_cluster_model(clustering_matrix, topic_model, n_topics, min_topic_size,
                                   options=options.get("cluster_options"), progress_callback=progress_callback)
        labels = np.asarray(fitted["labels"], dtype=int)
        notices.extend(fitted.get("warnings", []))
    else:
        fitted = fit_topic_model(documents, tfidf, terms, matrix if embedding != "tfidf" else None,
                                 topic_model, n_topics, min_topic_size, progress_callback=progress_callback)
        labels = np.asarray(fitted["labels"], dtype=int)
        notices.extend(fitted.get("warnings", []))
    if topic_model == "lda":
        # The public lexical mode is named tfidf, but LDA fits integer counts.
        # Retain the separate TF-IDF role in keyword extraction explicitly.
        keyword_representation = embedding_model
        embedding_model = "CountVectorizer / integer unigrams + bigrams"
        embedding_details.update(strategy="integer_counts_for_lda", model_id=embedding_model,
                                 keyword_representation=keyword_representation)
    outlier_label = fitted.get("outlier_label")
    map_matrix = fitted["map_matrix"]
    map_input = topic_model if topic_model in {"nmf", "lda"} else embedding
    map_representation = {"nmf": "nmf_topic_distribution", "lda": "lda_topic_distribution"}.get(
        topic_model, "tfidf" if embedding == "tfidf" else "sbert_embeddings")
    model_details = deepcopy(fitted["details"])
    if topic_model in CLUSTER_MODELS:
        model_details["requested_topics"] = n_topics
    del documents
    if progress_callback:
        progress_callback("全件の年次・引用・キーワード推移を集計")
    for paper, label in zip(selected, labels):
        paper["topic_id"] = f"topic-{label + 1}"
        paper["is_outlier"] = bool(outlier_label is not None and int(label) == int(outlier_label))
    by_year = Counter(p["year"] for p in selected)
    totals = [by_year[year] for year in years]
    topics = _build_topics(selected, labels, tfidf, terms, years, horizon, totals,
                           keyword_override=fitted.get("topic_keywords"), outlier_label=outlier_label,
                           information_insufficient_label=(model_details.get("information_insufficient_label")
                               if topic_model != "bertopic" and not model_details.get("noise_count") else None),
                           annual_comparison=annual_comparison)
    field_geometry = (store_nmf_geometry(selected, topics, fitted.get("membership"), model_details)
                      if topic_model == "nmf" else None)
    keyword_rows, keyword_count = _build_keywords(selected, tfidf, terms, years, annual_comparison=annual_comparison)
    # The map representation keeps its own reference (TF-IDF for KMeans, small
    # topic weights for NMF/LDA). Release the other lexical buffers before the
    # full-corpus author/frontier aggregation allocates its working structures.
    del tfidf, matrix
    if progress_callback:
        progress_callback("全件から月次変化・隣接領域の候補を計算")
    frontiers = _frontier_analysis(selected, topics, keyword_rows,
                                  {**options, "start_year": first_year, "end_year": last_year})
    unclassified_count = sum(p["is_outlier"] for p in selected)
    classified_topic_count = sum(not topic["is_outlier"] for topic in topics)
    if progress_callback:
        progress_callback("全件の共著者・所属ネットワークを集計")
    network, author_count = _build_network(selected, topics=topics)
    citation_rows = _citation_events(selected, years)
    timeline = [{"year": year, "papers": totals[i],
                 "citations": sum(p["citations"] or 0 for p in selected if p["year"] == year),
                 "citation_known_count": sum(p["citations"] is not None for p in selected if p["year"] == year),
                 **citation_rows[i]} for i, year in enumerate(years)]
    growth = 100 * (totals[-1] - totals[-2]) / totals[-2] if annual_comparison and totals[-2] else None
    methodology = [
        "対象は指定年に出版された取り込み済み論文です。年ごとの件数 0 は取り込み集合内の 0 であり、Scopus 全体に論文がないことを意味しません。検索式・収録遅延・輸入範囲の偏りを確認してください。",
        "タイトル・抄録・キーワードを結合し、選択した文書表現とトピック分類法を全対象論文へ適用します。手法・モデルID・実際の分類数はメタデータに記録します。失敗時に別手法へ自動変更しません。",
        "TF-IDF は単語・二語連結、英語停止語、最大 6,000 特徴です。日本語の形態素分割は行わないため、多言語文書には Transformer を検討してください。Transformer はモデル最大長から特殊トークン分を除いた区間（最大 510 トークン）へ本文を重複なしで分割し、各区間の正規化埋め込みをトークン数で加重平均して再正規化します。",
        "Transformer は 1 文書 32,000 字、最大 12 区間、全体 40,000 区間までです。文字上限超過では先頭と末尾、区間上限超過では全文に分散した区間を抽出します。上限による部分抽出は警告し、実際の区間数・対象文書数をメタデータへ記録します。これは抄録全体を学習する専用モデルの代替ではありません。",
        "技術マップは最大 400 論文をトピック・出版年が分散するよう抽出し、その表示集合全体を auto・PCA・t-SNE・UMAP の選択法で二次元へ投影します。auto は通常 t-SNE、少数・重複文書では線形 SVD / PCA を選びます。高次元の文書表現は SVD で最大 50 次元へ縮約して正規化し、再投影用に保存します。年・四半期・月の層別表示では同じ座標を使い、期間別の再学習は行いません。t-SNE は cosine 距離・PCA 初期化・seed=42・最大 750 反復・perplexity 最大 30、UMAP は cosine 距離・近傍最大 15・min_dist=0.1・seed=42 です。",
        "t-SNE は局所的な文書の近傍関係を見やすくする表示です。遠いトピック同士の距離、島の面積・密度、軸に技術成熟度や成功確率の意味はありません。表示標本が変わると配置も変わります。辺は地図に入力した文章表現または文書–トピック分布による表示文書間の cosine 近傍で、引用関係ではありません。トピック分類・件数・予測は表示座標によらず全対象論文で計算します。",
        "共著者の辺は実際に同じ論文に記載された著者間の共著件数です。最多 120 著者・240 辺を表示し、表示著者間の全辺で共著コミュニティを計算します。明示ID・明示別名を優先し、ID欠測の名前一致は推定として扱います。所属は各著者へ明示されたものだけを使います。",
        "累積引用は取り込み時点の Cited by 合計です。出版年別累積引用は受領年別の推移ではありません。年別引用は追加された受領年ごとの新規引用数のみを使用し、欠測を 0 に置換しません。対象期間より古い論文への引用は含みません。",
        "トピック・キーワード増減率は直近 2 年とその前の 2 年の年平均件数差を、前年期平均+1で割った平滑化率です。期間が短い場合は各 1 年です。全体の増減率は最終年対前年の通常の前年比（前年 0 は未定義）です。",
        "注目スコアは検証済みの技術成功確率ではありません。0.6×[0.5+0.5×tanh(平滑化増減率/100)]＋0.4×√直近期シェアを 100 倍した仮説用指標です。既刊の同一論文群の直近 2 年引用履歴が全件揃う場合のみ、その引用増減成分を 25% 加重します。引用成分は 50+50×tanh(分母+1の引用増減率/100) です。",
        "比較可能な引用履歴がない場合、累積引用÷出版経過年（分析年−出版年+1）の文書平均を注目度の代理値にします。100×[1−exp(−代理値/5)] をスコアの 15% に使います。これは引用の時系列増ではなく、出版月・引用取得日・分野・自己引用を補正しない便宜的な値です。引用値が全件欠測の場合は引用成分を使いません。累積引用の現在値は数値予測・予測検証に使用しません。",
        "emerging は平滑化増減率 25% 以上・直近 3 件以上・論文シェアが 0.5 ポイント超上昇の候補です。hot / stable / declining も論文活動を分類する便宜的な閾値で、技術成熟度や商業的成功の認定ではありません。",
        "予測は年間論文件数に対する log1p の Ridge 直線（傾きペナルティ 2）、減衰率 0.75、最終実測値との混合です。上限は直近 3 年最大件数×(1+0.6×先年数)+2×先年数。年 1 回の rolling-origin 検証で前年踏襲の MAE が小さければ前年踏襲を採用します。",
        "検証は最初の 3 年を訓練にして、各時点までの論文件数だけで翌年を予測します。ただしトピック抽出・ラベルは全対象期間を使用した回顧的分類です。これはトピック発見を含む完全な将来予測検証ではなく、短い 5 年系列では通常 2 検証点しかありません。",
        "予測の上下限は残差と件数規模から広げた探索的なシナリオ帯で、校正済みの信頼区間・予測確率ではありません。LLM が追加する将来技術の説明は出典論文に基づく仮説として検証が必要です。",
        "LLM 用の根拠論文は各トピックで最大 5 件、累積引用上位から 3 件と最新出版年から 2 件を重複なしで交互に選びます。同順位では抄録がある論文を優先します。引用された旧来技術と新しい報告の両方を含めるための選択で、網羅的な文献レビューではありません。",
        "著者キーワードが欠ける論文は上位 TF-IDF 語で補います。キーワード件数は論文単位の出現数です。累積引用の表示合計は補正せず、分野をまたぐ品質比較には使用できません。",
        "月次比較は出版月が確認できる論文を対象に、基準月までの 1・3・6 か月と、その直前の同月数を比べます。既定の基準月は最新完了月と分析終了年 12 月の早い方です。出版月不明の論文を配分せず、比較月が未取得と分かる場合は増減判定を行いません。CSV 等で取得範囲が不明な場合はその制約を表示します。",
        "疎な語の組合せは、全対象論文のタイトル・抄録・キーワード内の実際の語句出現を調べます。各語 3 件以上・独立仮定の期待共起数 nA×nB/N が 2 以上・実際の共起が 2 以下かつ期待の半分未満を候補とします。地図上の空白や密度は使用せず、組合せの新規性・有望性・因果関係を示す値ではありません。",
    ]
    topic_methodology = {
        "kmeans": "K-means はランダム初期化で文書表現を単一クラスタへ割り当てます。MiniBatch は別の選択肢です。代表語はクラスタ内の平均 TF-IDF 上位語で、乱数 seed=42 とします。",
        "nmf": "NMF は非負の TF-IDF 行列を文書–トピック重みとトピック–語重みに分解します。各論文を最大のトピック重みに割り当て、代表語にはモデルのトピック–語重み上位を使います。地図は文書ごとに合計 1 へ正規化した成分重みの表示です。これは確率でも SBERT の意味埋め込みでもありません。",
        "lda": "LDA は共通の語彙抽出条件で作った単語出現回数を使う確率的トピックモデルです。論文は最大の事後トピック確率へ割り当て、代表語は学習したトピック–語分布の上位語です。地図は文書–トピック確率を投影します。TF-IDF は選択する入力モード名ですが、LDA の学習自体には回数を使います。",
        "bertopic": "BERTopic は SBERT 埋め込みを UMAP で縮約し HDBSCAN で密度により分類します。代表語には分類結果の c-TF-IDF を使用します。指定トピック数は削減目標で、K-means の固定クラスタ数と同じ意味ではありません。表示地図は元の SBERT 埋め込みを別途選択した投影法で二次元化したもので、内部 UMAP の座標を流用しません。",
    }
    methodology.insert(2, topic_methodology.get(topic_model,
        CLUSTER_MODELS.get(topic_model, {}).get("description", "文書表現に対するクラスタリングです。")))
    if topic_model in CLUSTER_MODELS:
        methodology.append("クラスタリングと地図の入力は全論文で学習した最大50次元の表現をL2正規化したものです。必要時にSVDで縮約します。分類用の論文標本への間引きは行いません。")
    if field_geometry is not None:
        methodology.append("分野詳細の隣接度は、全対象論文の正規化 NMF 文書–トピック重みを群内平均した重心の cosine 類似度です。全文書の成分重みと群間類似度を保存し、2D地図距離から近さを推定しません。重みは確率ではなく、同じモデル内の比較に限ります。")
    if embedding != "tfidf":
        methodology.insert(3, f"文書埋め込みの実モデルIDは {embedding_model} です。保存した SBERT プリセットは環境変数で変更しません。旧 transformer 指定時のみ ATLAS_EMBEDDING_MODEL の互換設定を許容します。")
    if unclassified_count:
        message = f"未分類・情報不足の {unclassified_count} 件を保持しています。トピック数は分類済み群だけを数え、未分類群を注目技術・月次増加候補・予測として評価しません。"
        notices.append(message)
        methodology.append(message)
    if progress_callback:
        progress_callback(f"代表 {min(MAP_LIMIT, len(selected))} 件の技術マップを作成（集計は全件）")
    requested_projection = options.get("map_projection", "auto")
    technology_map = (_build_map(selected, labels, map_matrix, map_input)
                      if requested_projection == "auto" else
                      _build_map(selected, labels, map_matrix, map_input, requested_projection))
    if topic_model in CLUSTER_MODELS:
        technology_map["method"] = "全件の共通表現（必要時 SVD≤50・L2正規化） / " + technology_map["method"]
    # Save a common whole-corpus basis independently of the display sample.
    # Rows stay with their indexed paper payload so summary API responses can
    # remain bounded and the full landscape can stream them on demand.
    full_vectors, full_reduction = _projection_inputs(map_matrix)
    for start in range(0, len(selected), LEXICAL_BATCH_SIZE):
        for paper, vector in zip(selected[start:start + LEXICAL_BATCH_SIZE],
                                 full_vectors[start:start + LEXICAL_BATCH_SIZE]):
            paper["landscape_vector"] = np.round(vector, 8).tolist()
    landscape_representation = {"source": "saved_full_corpus_representation", "embedding": map_input,
        "dimensions": int(full_vectors.shape[1]), "original_dimensions": int(map_matrix.shape[1]),
        "reduction": full_reduction, "basis_scope": "full_corpus", "paper_count": len(selected)}
    del full_vectors
    return {"meta": {"start_year": first_year, "end_year": last_year, "years": years,
                     "single_year_corpus": single_year_corpus, "annual_comparison_available": annual_comparison,
                     "embedding": embedding, "embedding_model": embedding_model, "forecast_horizon": horizon,
                     "embedding_details": embedding_details, "embedding_model_preset": selection["preset"],
                     "sbert_model": selection["preset"] if embedding == "sbert" else None,
                     "topic_model": topic_model, "topic_model_details": model_details,
                     "cluster_options": deepcopy(options.get("cluster_options") or {}),
                     "landscape_representation": landscape_representation,
                     "analysis_scope": "full_corpus", "dataset_limit": MAX_DATASET_PAPERS,
                     "map_display_limit": MAP_LIMIT, "large_corpus": len(selected) > LARGE_CORPUS_THRESHOLD,
                     "map_representation": map_representation, "min_topic_size": min_topic_size,
                     "map_projection": options.get("map_projection", "auto"),
                     "unclassified_papers": unclassified_count, "classified_topics": classified_topic_count,
                     "paper_count": len(selected), "abstract_coverage": round(abstract_coverage, 2),
                     "citation_history_coverage": round(citation_history_coverage, 2),
                     "citation_snapshot_coverage": round(snapshot_coverage, 2), "warnings": notices, "is_demo": False},
            "summary": {"papers": len(selected), "authors": author_count, "keywords": keyword_count,
                        "citations": sum(p["citations"] or 0 for p in selected), "topics": classified_topic_count,
                        "unclassified_papers": unclassified_count,
                        "emerging_topics": sum(t["status"] == "emerging" for t in topics) if annual_comparison else 0,
                        "growth_pct": round(growth, 2) if growth is not None else None},
            "timeline": timeline, "topics": topics, "keywords": keyword_rows, "network": network,
            "top_cited_papers": sorted((paper for paper in selected if paper["citations"] is not None),
                key=lambda paper: (-paper["citations"], -paper["year"], paper["id"]))[:6],
            "map": technology_map, "papers": selected,
            "frontiers": frontiers, "field_geometry": field_geometry,
            "methodology": methodology}
