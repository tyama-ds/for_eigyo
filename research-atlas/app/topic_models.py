"""Bounded topic models with explicit missing-information and noise groups.

The returned compact labels are for counting papers. For NMF/LDA, membership
columns retain *all* fitted latent components, including components that are not
the dominant one for any paper; details.label_to_component explains the mapping.
No outlier reassignment or implicit change of algorithm is performed.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import warnings as python_warnings

import numpy as np
from scipy import sparse
from sklearn.decomposition import LatentDirichletAllocation, MiniBatchNMF, NMF
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS

from .limits import MAX_DATASET_PAPERS


LARGE_CORPUS_THRESHOLD = 20000
SEMANTIC_PAPER_LIMIT = 20000
LEXICAL_BATCH_SIZE = 2048
NMF_TRAINING_PASSES = 3
LDA_TRAINING_PASSES = 2


# Keep the lexical analyzer identical to analytics._tfidf. This is not a Japanese
# morphological tokenizer and does not merge synonyms or abbreviations.
STOP_WORDS = sorted(set(ENGLISH_STOP_WORDS) | {
    "abstract", "paper", "papers", "study", "studies", "research", "results",
    "result", "proposed", "propose", "using", "used", "use", "based", "method",
    "methods", "approach", "show", "shown", "new", "also", "however", "et", "al",
    "copyright", "elsevier", "rights", "reserved",
})
TOKEN_PATTERN = r"(?u)\b[^\W\d_][\w-]+\b"
LEXICAL_WARNING = "語彙モデルは英語向けの単語・2語句分割です。日本語の形態素解析、同義語・略語の統合は行いません。"


class TopicModelError(ValueError):
    """Readable configuration/runtime failure; never silently selects a model."""


def _positive_integer(value, name, minimum=1, maximum=20):
    try:
        parsed = int(value)
        if isinstance(value, bool) or float(value) != parsed or not minimum <= parsed <= maximum:
            raise ValueError
        return parsed
    except (ValueError, TypeError, OverflowError):
        raise TopicModelError(f"{name}は {minimum}〜{maximum} の整数で指定してください。") from None


def _count_vectorizer(vocabulary=None):
    return CountVectorizer(stop_words=STOP_WORDS, ngram_range=(1, 2), max_features=6000,
                           strip_accents="unicode", token_pattern=TOKEN_PATTERN, dtype=np.int32,
                           vocabulary=vocabulary)


def _finite_nonnegative(matrix, name):
    data = matrix.data if sparse.issparse(matrix) else np.asarray(matrix)
    if not np.isfinite(data).all() or (data < 0).any():
        raise TopicModelError(f"{name}に負数または有限でない値が含まれています。")


def _distinct_rows(matrix, limit=None):
    """Avoid an N x N distance matrix and dense conversion for large corpora."""
    if not sparse.issparse(matrix):
        return len(np.unique(np.round(matrix, 10), axis=0))
    matrix = matrix.tocsr()
    fingerprints = set()
    for index in range(matrix.shape[0]):
        start, end = matrix.indptr[index:index + 2]
        digest = hashlib.blake2b(digest_size=16)
        digest.update(matrix.indices[start:end].tobytes())
        digest.update(np.round(matrix.data[start:end], 10).tobytes())
        fingerprints.add(digest.digest())
        if limit is not None and len(fingerprints) >= limit:
            break
    return len(fingerprints)


def _compact(raw_labels):
    counts = Counter(int(label) for label in raw_labels if label >= 0)
    ordered = sorted(counts, key=lambda label: (-counts[label],
                     int(np.flatnonzero(raw_labels == label)[0])))
    mapping = {old: new for new, old in enumerate(ordered)}
    outlier_label = len(ordered) if np.any(raw_labels < 0) else None
    labels = np.array([mapping[int(label)] if label >= 0 else outlier_label
                       for label in raw_labels], dtype=int)
    return labels, mapping, outlier_label


def _representative_words(weights, terms, limit=6):
    chosen = []
    for index in np.argsort(-np.asarray(weights), kind="stable"):
        if weights[index] <= 0 or not np.isfinite(weights[index]):
            continue
        word = str(terms[index]).strip()
        if not word or word == "情報不足":
            continue
        tokens = set(word.split())
        if any(tokens <= set(other.split()) or set(other.split()) <= tokens for other in chosen):
            continue
        chosen.append(word)
        if len(chosen) >= limit:
            break
    return chosen


def _information_only(n, method, requested, warnings, map_matrix=None):
    warnings.append(f"有効な分析情報がない {n} 件を情報不足として保持しました。トピックを推定していません。")
    return {"labels": np.zeros(n, dtype=int),
            "map_matrix": np.zeros((n, 1)) if map_matrix is None else map_matrix,
            "topic_keywords": {0: []}, "outlier_label": 0, "membership": None,
            "warnings": warnings,
            "details": {"method": method, "random_state": 42, "requested_topics": requested,
                        "fitted_components": 0, "effective_topics": 0, "outlier_count": n,
                        "information_insufficient_count": n, "information_insufficient_label": 0,
                        "membership_description": "有効な情報がなく所属度は未計算です。",
                        "label_to_component": {}, "status": "information_insufficient"}}


def _large_lexical_fit(matrix, method, effective, progress_callback=None):
    """Visit every informative paper on each pass, then infer every membership.

    Batches are shuffled with a fixed seed instead of using the (possibly
    chronological) file order. No dense paper-by-vocabulary array is allocated.
    """
    if method == "nmf":
        estimator = MiniBatchNMF(n_components=effective, init="nndsvda", random_state=42,
                                batch_size=LEXICAL_BATCH_SIZE, max_iter=100,
                                transform_max_iter=100, tol=1e-4)
        passes = NMF_TRAINING_PASSES
    else:
        estimator = LatentDirichletAllocation(n_components=effective, random_state=42,
                    learning_method="online", batch_size=LEXICAL_BATCH_SIZE,
                    total_samples=matrix.shape[0], max_iter=1, max_doc_update_iter=30,
                    n_jobs=1, evaluate_every=-1)
        passes = LDA_TRAINING_PASSES
    rng = np.random.default_rng(42)
    batches = 0
    for epoch in range(passes):
        order = rng.permutation(matrix.shape[0])
        for start in range(0, len(order), LEXICAL_BATCH_SIZE):
            estimator.partial_fit(matrix[order[start:start + LEXICAL_BATCH_SIZE]])
            batches += 1
            if progress_callback:
                progress_callback(f"{method.upper()} 学習 {epoch + 1}/{passes} 周・"
                                  f"{min(start + LEXICAL_BATCH_SIZE, len(order)):,}/{len(order):,} 件")
    weights = np.empty((matrix.shape[0], effective), dtype=np.float32)
    for start in range(0, matrix.shape[0], LEXICAL_BATCH_SIZE):
        stop = min(start + LEXICAL_BATCH_SIZE, matrix.shape[0])
        weights[start:stop] = estimator.transform(matrix[start:stop])
        if progress_callback:
            progress_callback(f"{method.upper()} 全件の所属度を計算 {stop:,}/{matrix.shape[0]:,} 件")
    return estimator, weights, {"estimator": type(estimator).__name__,
        "training_mode": "full_corpus_minibatch", "training_passes": passes,
        "training_batches": batches, "batch_size": LEXICAL_BATCH_SIZE,
        "training_papers": matrix.shape[0], "transform_papers": matrix.shape[0],
        "sampling": "none", "shuffle_seed": 42,
        "iterations": passes, "max_iter": passes,
        "document_update_limit": 100 if method == "nmf" else 30}


def _fit_lexical(documents, tfidf, terms, method, requested, progress_callback=None):
    notices = [LEXICAL_WARNING]
    large = len(documents) > LARGE_CORPUS_THRESHOLD
    if method == "nmf":
        if tfidf is None or getattr(tfidf, "ndim", 0) != 2 or tfidf.shape[0] != len(documents):
            raise TopicModelError("NMF の TF-IDF 行列と論文数が一致しません。")
        terms = np.asarray(terms, dtype=str)
        if len(terms) != tfidf.shape[1]:
            raise TopicModelError("NMF の TF-IDF 列数と語彙数が一致しません。")
        _finite_nonnegative(tfidf, "TF-IDF")
        # analytics uses this sentinel for empty rows; it is metadata, not a word.
        keep = (terms != "情報不足") & (terms != "")
        # Column selection already owns its storage; do not duplicate a second
        # full-corpus sparse matrix merely to normalize its CSR representation.
        matrix = sparse.csr_matrix(tfidf[:, keep], dtype=np.float32, copy=False)
        vocabulary = terms[keep]
        input_name = "tfidf"
    else:
        # For a large corpus reuse the exact full-corpus TF-IDF vocabulary.
        # LDA still receives integer occurrence counts, never TF-IDF weights.
        vocabulary = {str(term): index for index, term in enumerate(
            term for term in terms if str(term) not in {"", "情報不足"})} if large else None
        vectorizer = _count_vectorizer(vocabulary=vocabulary or None)
        try:
            if large and vocabulary:
                matrix = sparse.vstack([vectorizer.transform(documents[start:start + LEXICAL_BATCH_SIZE])
                    for start in range(0, len(documents), LEXICAL_BATCH_SIZE)], format="csr")
            else:
                matrix = vectorizer.fit_transform(documents)
            vocabulary = vectorizer.get_feature_names_out()
        except ValueError as error:
            if "empty vocabulary" not in str(error):
                raise
            return _information_only(len(documents), method, requested, notices)
        input_name = "integer_term_counts"
    matrix.sum_duplicates()
    matrix.eliminate_zeros()
    matrix.sort_indices()
    informative = np.asarray(matrix.sum(axis=1)).ravel() > 0
    if not informative.any():
        return _information_only(len(documents), method, requested, notices)
    usable = matrix if informative.all() else matrix[informative]
    effective = min(requested, usable.shape[0], usable.shape[1], _distinct_rows(usable, limit=requested))
    if effective < requested:
        notices.append(f"有効な文書数・語彙数・異なる文書表現数に合わせ、{method.upper()} の成分数を {effective} に調整しました。")
    training_details = {}
    if method == "nmf" and not large:
        estimator = NMF(n_components=effective, init="nndsvda", random_state=42, max_iter=300,
                        solver="cd", tol=1e-4)
    elif not large:
        estimator = LatentDirichletAllocation(n_components=effective, random_state=42,
                     learning_method="batch", max_iter=20, n_jobs=1, evaluate_every=-1)
    with python_warnings.catch_warnings(record=True) as captured:
        python_warnings.simplefilter("always", ConvergenceWarning)
        if large:
            estimator, weights, training_details = _large_lexical_fit(
                usable, method, effective, progress_callback)
        else:
            weights = np.asarray(estimator.fit_transform(usable), dtype=np.float32)
            training_details = {"estimator": type(estimator).__name__, "training_mode": "batch",
                                "training_papers": usable.shape[0], "transform_papers": usable.shape[0],
                                "sampling": "none", "iterations": int(estimator.n_iter_),
                                "max_iter": 300 if method == "nmf" else 20}
    if large:
        notices.append(f"{len(documents):,} 件のため {training_details['estimator']} を使い、"
                       f"情報のある全 {usable.shape[0]:,} 件を {training_details['training_passes']} 周の分割学習・全件分類に使用しました。"
                       "論文を標本に間引いていません。通常の一括学習とは最適化手順が異なるため、分野の分け方が変わる場合があります。")
    if any(issubclass(item.category, ConvergenceWarning) for item in captured):
        notices.append(f"{method.upper()} は反復上限に達しました。語彙やトピック数を変更すると結果が変わる可能性があります。")
    _finite_nonnegative(weights, "文書トピック重み")
    _finite_nonnegative(estimator.components_, "トピック語彙重み")
    sums = weights.sum(axis=1)
    weights /= np.maximum(sums, np.finfo(weights.dtype).tiny)[:, None]
    full_weights = np.zeros((len(documents), effective), dtype=np.float32)
    full_weights[informative] = weights
    assigned = informative.copy()
    assigned[np.flatnonzero(informative)[sums <= 0]] = False
    raw_labels = np.full(len(documents), -1, dtype=int)
    raw_labels[assigned] = full_weights[assigned].argmax(axis=1)
    labels, mapping, outlier = _compact(raw_labels)
    keywords = {new: _representative_words(estimator.components_[old], vocabulary)
                for old, new in mapping.items()}
    missing_count = int((~assigned).sum())
    if outlier is not None:
        keywords[outlier] = []
        notices.append(f"語彙またはトピック重みが得られない {missing_count} 件を情報不足として保持しました。所属度は未計算（全成分 0）です。")
    if len(mapping) < effective:
        notices.append(f"{effective} 成分のうち、最大重みを持つ文書がある {len(mapping)} トピックを集計表示します。マップ用の分布には全成分を保持します。")
    description = ("NMF 成分重みを文書ごとに合計 1 に正規化した値です。確率や予測精度ではありません。"
                   if method == "nmf" else "LDA が推定した文書内のトピック混合比です。分類の正解確率や予測精度ではありません。")
    return {"labels": labels, "map_matrix": full_weights, "topic_keywords": keywords,
            "outlier_label": outlier, "membership": full_weights, "warnings": notices,
            "details": {"method": method, "random_state": 42, "requested_topics": requested,
                        "input": input_name, "fitted_components": effective,
                        "effective_topics": len(mapping), "outlier_count": missing_count,
                        "information_insufficient_count": missing_count,
                        "information_insufficient_label": outlier, "vocabulary_size": len(vocabulary),
                        **training_details, "matrix_dtype": str(matrix.dtype),
                        "membership_dtype": str(full_weights.dtype),
                        "corpus_papers": len(documents),
                        "membership_description": description,
                        "label_to_component": {str(new): old for old, new in mapping.items()},
                        "map_representation": "normalized_document_topic_weights",
                        "label_representation": "component_word_weights", "status": "ok"}}


def _bertopic_dependencies():
    try:
        from bertopic import BERTopic
        from bertopic.vectorizers import ClassTfidfTransformer
        from hdbscan import HDBSCAN
        from umap import UMAP
        return BERTopic, UMAP, HDBSCAN, ClassTfidfTransformer
    except (ImportError, OSError, RuntimeError) as error:
        raise TopicModelError("BERTopic の追加依存（bertopic・umap-learn・hdbscan）を読み込めません。"
                              "requirements-topics.txt と実行環境を確認してください。") from error


def _fit_bertopic(documents, embeddings, requested, minimum_size):
    if embeddings is None:
        raise TopicModelError("BERTopic には Transformer の文書埋め込みが必要です。")
    vectors = np.asarray(embeddings, dtype=float)
    if vectors.ndim != 2 or vectors.shape[0] != len(documents) or vectors.shape[1] < 2:
        raise TopicModelError("BERTopic の埋め込み行列は、論文数と一致する 2 次元以上の行列が必要です。")
    if not np.isfinite(vectors).all():
        raise TopicModelError("BERTopic の埋め込みに有限でない値が含まれています。")
    notices = [LEXICAL_WARNING,
               "BERTopic のトピック数は目標上限です。UMAP＋HDBSCAN で得た群が多いときだけ縮約し、未分類を強制的に割り当てません。"]
    analyzer = _count_vectorizer().build_analyzer()
    informative = np.array([bool(analyzer(document)) for document in documents])
    informative &= np.linalg.norm(vectors, axis=1) > 0
    if not informative.any():
        return _information_only(len(documents), "bertopic", requested, notices, vectors.copy())
    available = int(informative.sum())
    required = max(5, minimum_size + 1)
    if available < required:
        raise TopicModelError(f"BERTopic は現在の最小トピックサイズ {minimum_size} では、有効な論文が少なくとも {required} 件必要です（現在 {available} 件）。")
    BERTopic, UMAP, HDBSCAN, ClassTfidfTransformer = _bertopic_dependencies()
    selected_documents = [document for document, keep in zip(documents, informative) if keep]
    selected_vectors = vectors[informative].copy()
    selected_vectors /= np.linalg.norm(selected_vectors, axis=1)[:, None]
    if _distinct_rows(selected_vectors) < 2:
        # UMAP's randomized layout must not turn identical semantic vectors into
        # apparently different scientific topics.
        raw_labels = np.full(len(documents), -1, dtype=int)
        topic_words = {}
        initial_topics = 0
        reduction = False
        notices.append("すべての有効な埋め込みが同一で、意味的に異なる群を識別できません。全件を未分類として保持しました。")
    else:
        umap_model = UMAP(n_neighbors=min(15, available - 1), n_components=min(5, available - 2),
                          min_dist=0.0, metric="cosine", random_state=42, transform_seed=42,
                          init="random", n_jobs=1, low_memory=True)
        hdbscan_model = HDBSCAN(min_cluster_size=minimum_size, min_samples=min(5, minimum_size),
                               metric="euclidean", cluster_selection_method="eom",
                               prediction_data=False, core_dist_n_jobs=1)
        model = BERTopic(embedding_model=None, umap_model=umap_model,
                        hdbscan_model=hdbscan_model, vectorizer_model=_count_vectorizer(),
                        ctfidf_model=ClassTfidfTransformer(), nr_topics=None,
                        calculate_probabilities=False, top_n_words=12, verbose=False)
        try:
            fitted_labels, _ = model.fit_transform(selected_documents, embeddings=selected_vectors)
            fitted_labels = np.asarray(fitted_labels, dtype=int)
            initial_topics = len(set(fitted_labels) - {-1})
            reduction = initial_topics > requested
            if reduction:
                # BERTopic's nr_topics includes its -1 group; our limit counts
                # only classified topics, so reserve a separate noise slot.
                model.reduce_topics(selected_documents, nr_topics=requested + int(-1 in fitted_labels))
                fitted_labels = np.asarray(model.topics_, dtype=int)
            if fitted_labels.shape != (available,) or (fitted_labels < -1).any():
                raise TopicModelError("BERTopic が有効な論文ごとのラベルを返しませんでした。")
            topic_words = {}
            for topic in set(fitted_labels) - {-1}:
                pairs = model.get_topic(int(topic)) or []
                terms = [str(word) for word, _ in pairs]
                scores = np.array([float(score) for _, score in pairs])
                if not np.isfinite(scores).all():
                    raise TopicModelError("BERTopic が有限でない語彙重みを返しました。")
                topic_words[int(topic)] = _representative_words(scores, terms)
        except TopicModelError:
            raise
        except Exception as error:
            raise TopicModelError("BERTopic の推定に失敗しました。語彙数、最小トピックサイズと追加依存の互換性を確認してください。") from error
        raw_labels = np.full(len(documents), -1, dtype=int)
        raw_labels[informative] = fitted_labels
    labels, mapping, outlier = _compact(raw_labels)
    keywords = {new: topic_words.get(old, []) for old, new in mapping.items()}
    outlier_count = int((raw_labels < 0).sum())
    information_count = len(documents) - available
    if outlier is not None:
        keywords[outlier] = []
        notices.append(f"BERTopic で分類できない {outlier_count} 件を未分類として保持しました。これは単一の研究テーマではありません。")
    if information_count:
        notices.append(f"うち {information_count} 件は有効な語彙または埋め込みがなく、モデル推定から除外しました。論文総数には含めています。")
    return {"labels": labels, "map_matrix": vectors.copy(), "topic_keywords": keywords,
            "outlier_label": outlier, "membership": None, "warnings": notices,
            "details": {"method": "bertopic", "random_state": 42, "requested_topics": requested,
                        "topic_count_mode": "maximum_after_reduction", "effective_topics": len(mapping),
                        "initial_topics": initial_topics, "reduced_topics": reduction,
                        "min_topic_size": minimum_size, "outlier_count": outlier_count,
                        "information_insufficient_count": information_count,
                        "information_insufficient_label": outlier if information_count == outlier_count and information_count else None,
                        "umap": {"n_components": min(5, available - 2), "n_neighbors": min(15, available - 1),
                                 "metric": "cosine", "init": "random", "n_jobs": 1},
                        "hdbscan": {"min_cluster_size": minimum_size, "min_samples": min(5, minimum_size),
                                    "prediction_data": False, "metric": "euclidean"},
                        "membership_description": "全トピックへの所属確率は計算していません。HDBSCAN の未分類を保持します。",
                        "map_representation": "sentence_transformer_embeddings",
                        "label_representation": "class_tfidf", "label_to_component": {},
                        "status": "all_noise" if not mapping else "ok"}}


def fit_topic_model(documents, tfidf, terms, embeddings, method, n_topics, min_topic_size=5,
                    progress_callback=None):
    """Return a real NMF, LDA or BERTopic fit without dropping any input paper."""
    if method not in {"nmf", "lda", "bertopic"}:
        raise TopicModelError("トピックモデルは nmf・lda・bertopic のいずれかを指定してください。")
    if not isinstance(documents, (list, tuple)) or not documents or not all(isinstance(item, str) for item in documents):
        raise TopicModelError("文書は空ではない文字列のリストで指定してください（空文字の文書は情報不足として扱います）。")
    if len(documents) > MAX_DATASET_PAPERS:
        raise TopicModelError(f"トピックモデルの分析上限は {MAX_DATASET_PAPERS:,} 件です。")
    requested = _positive_integer(n_topics, "トピック数")
    if method == "bertopic":
        if len(documents) > SEMANTIC_PAPER_LIMIT:
            raise TopicModelError(f"BERTopic のローカル分析上限は {SEMANTIC_PAPER_LIMIT:,} 件です。"
                                  "対象期間を絞るか、全件を分析する NMF・LDA・TF-IDF + KMeans を選択してください。")
        minimum_size = _positive_integer(min_topic_size, "最小トピックサイズ", minimum=2, maximum=1000)
        return _fit_bertopic(documents, embeddings, requested, minimum_size)
    return _fit_lexical(documents, tfidf, terms, method, requested, progress_callback)
