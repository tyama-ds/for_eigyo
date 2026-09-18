"""Read-only patent projections and explicitly scoped review diagnostics.

The 2-D views are navigation aids, not a classifier or evidence of recall.
Inputs and user verdicts are never changed by these functions.
"""
import math
import warnings

import numpy as np
from scipy.linalg import orthogonal_procrustes
from sklearn.decomposition import PCA, TruncatedSVD
from sklearn.manifold import TSNE
from sklearn.metrics.pairwise import cosine_similarity

from analysis_engine import texts, vectorize
from llm_judgment import is_assessed
from workspace_import import publication_key

MAX_PATENTS = 5000


def _rows(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_PATENTS:
        raise ValueError('特許マップは5,000件まで対応しています。')
    ids = [r.get('id') for r in rows if isinstance(r, dict)]
    if len(ids) != len(rows) or any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('特許IDに不足または重複があります。CSVを確認してください。')
    return rows


def _features(rows):
    docs = texts([dict(title=str(r.get('title') or ''), abstract=str(r.get('abstract') or '')) for r in rows])
    if not any(d.strip() for d in docs):
        raise ValueError('配置に使用できるタイトル・要約がありません。')
    return vectorize(docs)[1]


def _representation(x):
    # Never densify a 5,000 x 16,000 document/term matrix. PCA is performed
    # after a disclosed, reproducible SVD compression when it exceeds 64D.
    dimensions = min(64, x.shape[1], max(1, x.shape[0] - 1))
    if x.shape[1] <= 64:
        return x.toarray(), '文字TF-IDF（最大64特徴）', None
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        svd = TruncatedSVD(n_components=dimensions, random_state=42)
        dense = svd.fit_transform(x)
    variance = float(np.nansum(svd.explained_variance_ratio_))
    return dense, f'文字TF-IDFをSVDで{dimensions}次元に圧縮', variance


def _reference_map(reference):
    if reference is None:
        return {}
    if isinstance(reference, dict):
        reference = reference.get('points')
    if not isinstance(reference, list) or len(reference) > MAX_PATENTS:
        raise ValueError('参照する配置は5,000点以内で指定してください。')
    result = {}
    for p in reference:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or p['id'] in result:
            raise ValueError('参照する配置のIDが不正または重複しています。')
        if any(isinstance(p.get(k), bool) or not isinstance(p.get(k), (int, float))
               or not math.isfinite(p[k]) or abs(p[k]) > 1e6 for k in ('x', 'y')):
            raise ValueError('参照する配置には有限の座標を指定してください。')
        result[p['id']] = (p['x'], p['y'])
    return result


def _align_and_frame(coords, ids, reference):
    ref = _reference_map(reference)
    coords = np.asarray(coords, dtype=float)
    coords -= coords.mean(axis=0)
    common = [(i, ref[value]) for i, value in enumerate(ids) if value in ref]
    aligned = False
    if len(common) >= 3:
        a = coords[[i for i, _ in common]]
        b = np.asarray([point for _, point in common], dtype=float)
        a = a - a.mean(axis=0)
        b = b - b.mean(axis=0)
        if np.linalg.matrix_rank(a) == 2 and np.linalg.matrix_rank(b) == 2:
            rotation, _ = orthogonal_procrustes(a, b)
            coords = coords @ rotation
            aligned = True
    span = float(np.max(np.ptp(coords, axis=0)))
    center = (coords.min(axis=0) + coords.max(axis=0)) / 2
    # A uniform scale preserves the aspect ratio of each projection.
    framed = .5 + (coords - center) * (.84 / span if span > 1e-12 else 0)
    return framed, aligned


def project_patents(rows, method='pca', reference=None):
    rows = _rows(rows)
    if method not in ('pca', 'tsne', 'umap'):
        raise ValueError('配置方式は pca / tsne / umap から選んでください。')
    _reference_map(reference)
    if not rows:
        return dict(method=method, points=[], metadata=dict(count=0, note='特許CSVを取り込んでください。'))
    x = _features(rows)
    dense, representation, retained = _representation(x)
    metadata = dict(count=len(rows), representation=representation, dimensions=int(dense.shape[1]),
                    svd_retained_variance=retained, seed=42,
                    note='タイトル・要約の表現の類似度です。2D上の距離・境界は要否の証明ではなく、方式間で軸・距離の尺度は比較できません。')
    if method != 'pca' and len(rows) < 4:
        raise ValueError('t-SNE / UMAP は4件以上の特許で使用できます。少数の特許はPCAで確認してください。')
    if method == 'pca':
        if len(rows) == 1 or not np.any(np.var(dense, axis=0) > 1e-15):
            coords = np.zeros((len(rows), 2))
            metadata['degenerate'] = True
        else:
            components = min(2, dense.shape[1], len(rows))
            pca = PCA(n_components=components, svd_solver='full')
            coords = pca.fit_transform(dense)
            if components == 1:
                coords = np.column_stack((coords[:, 0], np.zeros(len(rows))))
            metadata['explained_variance_ratio'] = [float(v) for v in pca.explained_variance_ratio_]
        metadata['algorithm'] = '中心化した特徴表現のPCA'
    elif method == 'tsne':
        if not np.any(np.var(dense, axis=0) > 1e-15):
            raise ValueError('文書の特徴がすべて同じためt-SNEで区別できません。PCAで重なりを確認してください。')
        perplexity = min(30., max(1., (len(rows) - 1) / 3))
        coords = TSNE(n_components=2, random_state=42, perplexity=perplexity,
                      init='random', learning_rate='auto', max_iter=750).fit_transform(dense)
        metadata.update(algorithm='t-SNE', perplexity=perplexity)
    else:
        try:
            from umap import UMAP
        except ImportError:
            raise ValueError('UMAPの追加依存がありません。アプリのPython環境で pip install umap-learn を実行して再起動してください。現在の配置は保持します。') from None
        if not np.any(np.var(dense, axis=0) > 1e-15):
            raise ValueError('文書の特徴がすべて同じためUMAPで区別できません。PCAで重なりを確認してください。')
        neighbors = min(15, len(rows) - 1)
        coords = UMAP(n_components=2, random_state=42, n_neighbors=neighbors, init='random',
                      min_dist=.15, n_jobs=1).fit_transform(dense)
        metadata.update(algorithm='UMAP', n_neighbors=neighbors, min_dist=.15)
    if not np.isfinite(coords).all():
        raise ValueError('有限の配置を計算できませんでした。文書の内容を確認してください。')
    coords, aligned = _align_and_frame(coords, [r['id'] for r in rows], reference)
    metadata['orientation_aligned'] = aligned
    metadata['orientation_note'] = '共通IDに対して回転・反転のみを合わせます。個々の位置関係や尺度の一致は保証しません。'
    return dict(method=method, points=[dict(id=r['id'], x=round(float(c[0]), 7), y=round(float(c[1]), 7))
                                      for r, c in zip(rows, coords)], metadata=metadata)


def _finite_score(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def compute_evaluation(rows, target_ids=None):
    rows = _rows(rows)
    human = [r for r in rows if r.get('label_source') == 'human' and r.get('label') in ('keep', 'exclude')]
    machine = [r for r in rows if r.get('label_source') == 'agent' and r.get('label') in ('keep', 'exclude')]
    unknown_source = [r for r in rows if r.get('label_source') not in ('human', 'agent') and r.get('label') in ('keep', 'exclude')]
    keep = sum(r['label'] == 'keep' for r in human)
    exclude = len(human) - keep
    held = [r for r in rows if r.get('label') not in ('keep', 'exclude') and is_assessed(r)]
    targets = {}
    for identifier in target_ids or []:
        if isinstance(identifier, str):
            targets.setdefault(publication_key({'id': identifier}), identifier)
    present = set(targets) & {publication_key(row) for row in rows}
    result = dict(count=len(rows), human_keep=keep, human_exclude=exclude, human_reviewed=len(human),
                  machine_keep=sum(r['label'] == 'keep' for r in machine),
                  machine_exclude=sum(r['label'] == 'exclude' for r in machine),
                  hold_count=len(held), unknown_source_count=len(unknown_source),
                  unreviewed_count=len(rows)-len(human)-len(machine)-len(unknown_source)-len(held),
                  signal_ratio=keep / len(human) if human else None,
                  signal_noise_ratio=keep / exclude if exclude else None,
                  sn_status='available' if exclude else ('no_excludes' if keep else 'no_human_reviews'),
                  target_count=len(targets), target_found=len(present), missing_target_ids=sorted(targets[key] for key in set(targets) - present),
                  scope_note='人が必要・不要を確定した部分集合のS/Nです。検索母集団全体の適合率・再現率や検索漏れを推定しません。',
                  boundary_note='境界候補は元の文字TF-IDF空間で必要例と不要例の両方に近い特許です。2D図の見た目の境界とは別です。',
                  boundary_candidates=[])
    candidates = {}
    for r in held:
        candidates[r['id']] = dict(id=r['id'], kind='llm_hold', priority=0, reason='LLMの保留判定を人が確認')
    for r in rows:
        value = r.get('score')
        if r.get('label_source') == 'human' or not _finite_score(value):
            continue
        if r.get('score_source') in ('lightweight', 'transformer') and .4 <= value <= .6:
            candidates.setdefault(r['id'], dict(id=r['id'], kind='model_uncertainty', priority=1,
                                               reason='学習モデルの関連度が0.5付近（確率の校正・独立検証とは別）', score=value))
    if keep and exclude and rows:
        x = _features(rows)
        keep_idx = [i for i, r in enumerate(rows) if r.get('label_source') == 'human' and r.get('label') == 'keep']
        exclude_idx = [i for i, r in enumerate(rows) if r.get('label_source') == 'human' and r.get('label') == 'exclude']
        for start in range(0, len(rows), 128):
            block = x[start:start + 128]
            pos = cosine_similarity(block, x[keep_idx])
            neg = cosine_similarity(block, x[exclude_idx])
            for offset in range(block.shape[0]):
                a, b = int(np.argmax(pos[offset])), int(np.argmax(neg[offset]))
                p, n = float(pos[offset, a]), float(neg[offset, b])
                r = rows[start + offset]
                if min(p, n) >= .1 and abs(p - n) <= .15:
                    candidates.setdefault(r['id'], dict(id=r['id'], kind='neighbor_disagreement', priority=2,
                        reason='必要例・不要例の双方に表現が近い', keep_neighbor=rows[keep_idx[a]]['id'],
                        exclude_neighbor=rows[exclude_idx[b]]['id'], keep_similarity=round(p, 4),
                        exclude_similarity=round(n, 4), gap=round(abs(p - n), 4)))
    result['boundary_candidates'] = sorted(candidates.values(), key=lambda c: (c['priority'], c.get('gap', 0), c['id']))[:100]
    result['boundary_candidate_count'] = len(candidates)
    result['boundary_candidates_shown'] = len(result['boundary_candidates'])
    return result
