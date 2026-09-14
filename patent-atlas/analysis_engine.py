import csv
import copy
import hashlib
import io
import re
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_recall_fscore_support
from sklearn.model_selection import GroupShuffleSplit

ALIASES = {
    'id': ['id', '公報番号', '公開番号', '公開・公表番号', '出願番号', 'publication number', 'publication_number'],
    'title': ['title', '発明の名称', '名称', 'タイトル'],
    'abstract': ['abstract', '要約', '抄録', '要約（日本語）', 'summary'],
    'applicant': ['applicant', 'assignee', '出願人', '出願人/権利者', 'assignee - original'],
    'ipc': ['ipc', '国際特許分類', 'ipc分類', 'ipc classifications'],
    'fi': ['fi', 'fi分類', 'fi分類記号', 'fi classifications', 'file index'],
    'fterm': ['fterm', 'f-term', 'fターム', 'fターム分類'],
    'year': ['year', '公開日', '出願日', 'publication date', 'publication_date'],
    'family': ['family', 'family id', 'family_id', 'ファミリーid'],
}

def parse_csv(raw, mapping=None):
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError('CSVは20MB以下にしてください。')
    text = None
    encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'cp932')
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except UnicodeError:
            continue
    if text is None:
        raise ValueError('文字コードを判定できません。UTF-8またはShift-JISで保存してください。')
    try:
        dialect = csv.Sniffer().sniff(text[:10000], delimiters=',\t;')
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    chosen = {}
    normalized = {h.strip().lower(): h for h in headers}
    for field, aliases in ALIASES.items():
        chosen[field] = (mapping or {}).get(field) or next((normalized[a.lower()] for a in aliases if a.lower() in normalized), None)
    if not chosen['title']:
        return {'needs_mapping': True, 'headers': headers, 'mapping': chosen, 'rows': []}
    rows, seen, duplicates, skipped = [], set(), 0, 0
    for n, row in enumerate(reader):
        if n >= 5000:
            raise ValueError('初版はCSV 5,000行まで対応しています。ファイルを分割してください。')
        item = {f: str(row.get(c) or '').strip()[:12000] for f, c in chosen.items()}
        if not item['title']:
            skipped += 1
            continue
        content_key = hashlib.sha256((item['title'] + item['abstract']).encode()).hexdigest()[:16]
        item['id'] = item['id'] or 'ROW-' + content_key
        if item['id'] in seen:
            duplicates += 1
            continue
        seen.add(item['id'])
        item.update(label=None, label_source=None, label_reason='', score=None, content_key=content_key)
        rows.append(item)
    if not rows:
        raise ValueError('発明の名称がある行が見つかりません。列の割り当てを確認してください。')
    return {'needs_mapping': False, 'headers': headers, 'mapping': chosen, 'rows': rows, 'duplicates': duplicates, 'skipped': skipped}

def backfill_fi(existing_rows, imported_rows):
    """Return a detached FI-only patch plus a report; never overwrite judgments.

    ID, title, abstract and any existing content key must all match. Ambiguous
    IDs and nonempty FI conflicts are reported and left alone. This function
    performs no persistence and must be committed under the caller's state lock.
    """
    result = copy.deepcopy(existing_rows)
    indexed = {}
    for row in imported_rows:
        indexed.setdefault(str(row.get('id', '')), []).append(row)
    report = dict(matched_count=0, updated_count=0, already_present_count=0,
                  updated_ids=[], conflicts=[], unmatched_ids=[], missing_fi_ids=[])
    seen_existing = Counter(str(row.get('id', '')) for row in existing_rows)
    for row in result:
        identifier = str(row.get('id', ''))
        incoming = indexed.get(identifier, [])
        if not incoming:
            report['unmatched_ids'].append(identifier)
            continue
        if len(incoming) != 1 or seen_existing[identifier] != 1:
            report['conflicts'].append(dict(id=identifier, reason='duplicate_id'))
            continue
        incoming = incoming[0]
        if any(str(row.get(field) or '') != str(incoming.get(field) or '') for field in ('title', 'abstract')):
            report['conflicts'].append(dict(id=identifier, reason='content_mismatch'))
            continue
        if row.get('content_key') and incoming.get('content_key') and row['content_key'] != incoming['content_key']:
            report['conflicts'].append(dict(id=identifier, reason='content_key_mismatch'))
            continue
        report['matched_count'] += 1
        value = incoming.get('fi') or ''
        if not isinstance(value, str) or not value.strip():
            report['missing_fi_ids'].append(identifier)
        elif row.get('fi'):
            if str(row['fi']).strip() != value.strip():
                report['conflicts'].append(dict(id=identifier, reason='existing_fi_differs'))
            else:
                report['already_present_count'] += 1
        else:
            row['fi'] = value.strip()
            report['updated_count'] += 1
            report['updated_ids'].append(identifier)
    return result, report


def texts(rows):
    return [(r['title'] + ' ' + r.get('abstract', ''))[:6000] for r in rows]

def vectorize(docs):
    v = TfidfVectorizer(analyzer='char', ngram_range=(2, 4), max_features=16000, sublinear_tf=True)
    try:
        x = v.fit_transform(docs)
    except ValueError:
        v = TfidfVectorizer(analyzer='char', ngram_range=(1, 1), max_features=16000)
        x = v.fit_transform(docs)
    return v, x

def map_patents(rows, keywords):
    docs = texts(rows)
    vectorizer, x = vectorize(docs)
    if len(rows) > 2 and x.shape[1] > 2:
        coords = TruncatedSVD(n_components=2, random_state=42).fit_transform(x)
        k = min(6, max(2, int(np.sqrt(len(rows) / 2))), len(rows))
        clusters = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(x)
    else:
        coords = np.array([[float(i), 0.] for i in range(len(rows))])
        clusters = np.zeros(len(rows), dtype=int)
    for axis in (0, 1):
        lo, hi = coords[:, axis].min(), coords[:, axis].max()
        coords[:, axis] = .12 + .76 * (coords[:, axis] - lo) / max(float(hi - lo), 1e-8) if hi > lo else .5
    label_vectorizer = TfidfVectorizer(token_pattern=r'(?u)[一-龯ァ-ヶー]{2,20}|[a-zA-Z][a-zA-Z0-9-]{2,30}', max_features=8000)
    try:
        label_x = label_vectorizer.fit_transform(docs)
        features = np.array(label_vectorizer.get_feature_names_out())
    except ValueError:
        label_x = x
        features = np.array(vectorizer.get_feature_names_out())
    summaries = {}
    for cluster in sorted(set(clusters.tolist())):
        indices = np.where(clusters == cluster)[0]
        weights = np.asarray(label_x[indices].mean(axis=0)).ravel()
        candidates = features[np.argsort(weights)[::-1][:50]].tolist()
        terms = []
        for t in candidates:
            if len(t.strip()) >= 3 and not re.search(r'[\s。、，,（）()]', t) and not any(t in old or old in t for old in terms):
                terms.append(t)
            if len(terms) == 3:
                break
        summaries[int(cluster)] = {'id': int(cluster), 'name': ' / '.join(terms) or '特許群', 'count': len(indices), 'terms': terms}
    keys = re.split(r'[\s,、;]+', keywords.strip())
    for i, r in enumerate(rows):
        matched = [w for w in keys if w and w.lower() in docs[i].lower()]
        r.update(x=round(float(coords[i, 0]), 5), y=round(float(coords[i, 1]), 5), cluster=int(clusters[i]), matched_keywords=matched,
                 map_reason='タイトル・要約の文字列類似度で配置。近い点ほど表現が似ています。',
                 inclusion_reason=('入力語一致: ' + '、'.join(matched)) if matched else '入力語の完全一致なし。検索元でのヒット理由はCSVだけでは確定できません。')
    return list(summaries.values())

def train(rows, mode, model_path, allow_download, output_dir, progress, cancelled, include_agent=False):
    labeled = [r for r in rows if r.get('label') in ('keep', 'exclude') and (include_agent or r.get('label_source') == 'human')]
    counts = Counter(r['label'] for r in labeled)
    minimum = 4 if mode == 'transformer' else 2
    if min(counts.get('keep', 0), counts.get('exclude', 0)) < minimum:
        raise ValueError(f'この学習には「必要」「不要」をそれぞれ{minimum}件以上指定してください。')
    docs, y = texts(labeled), np.array([int(r['label'] == 'keep') for r in labeled])
    groups = [r.get('family') or r.get('content_key') or r['id'] for r in labeled]
    train_idx, test_idx = np.arange(len(labeled)), np.array([], dtype=int)
    # A holdout score is only shown for entirely human-labelled datasets.
    if len(set(groups)) > 1 and min(counts.values()) >= 4 and all(r.get('label_source') == 'human' for r in labeled):
        for seed in range(30):
            a, b = next(GroupShuffleSplit(n_splits=1, test_size=.25, random_state=seed).split(docs, y, groups))
            if len(set(y[a])) == 2 and len(set(y[b])) == 2:
                train_idx, test_idx = a, b
                break
    progress(12, '学習用データを準備しています')
    if mode == 'transformer':
        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
        except ImportError:
            raise ValueError('Transformer依存がありません。requirements-transformer.txt をインストールしてください。') from None
        if not model_path:
            raise ValueError('設定タブにTransformerモデル名またはローカルモデルフォルダを指定してください。')
        torch.manual_seed(42)
        torch.set_num_threads(min(4, torch.get_num_threads()))
        progress(18, 'Transformerモデルを読み込み中（初回ダウンロードは時間がかかります）')
        try:
            tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=not allow_download, trust_remote_code=False)
            model = AutoModelForSequenceClassification.from_pretrained(model_path, num_labels=2, local_files_only=not allow_download, trust_remote_code=False, ignore_mismatched_sizes=True, use_safetensors=True)
        except Exception:
            raise ValueError('Transformerモデルを読み込めません。ローカルパス、safetensors形式、依存関係を確認してください。ダウンロード時は環境のHTTPS_PROXYも利用できます。') from None
        optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)
        model.train()
        total = 3 * int(np.ceil(len(train_idx) / 4))
        step = 0
        for epoch in range(3):
            shuffled = np.random.default_rng(42 + epoch).permutation(train_idx)
            for start in range(0, len(shuffled), 4):
                if cancelled():
                    raise ValueError('処理を停止しました。')
                batch = shuffled[start:start+4]
                inputs = tokenizer([docs[i] for i in batch], padding=True, truncation=True, max_length=256, return_tensors='pt')
                loss = model(**inputs, labels=torch.tensor(y[batch], dtype=torch.long)).loss
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                optimizer.zero_grad()
                step += 1
                progress(20 + int(55 * step / total), f'Transformer本体を微調整中 {step}/{total}')
        model.eval()
        def predict(items):
            result = []
            with torch.no_grad():
                for start in range(0, len(items), 8):
                    if cancelled():
                        raise ValueError('処理を停止しました。')
                    inputs = tokenizer(items[start:start+8], padding=True, truncation=True, max_length=256, return_tensors='pt')
                    result.extend(torch.softmax(model(**inputs).logits, dim=-1)[:, 1].tolist())
            return np.array(result)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        model.save_pretrained(output_dir, safe_serialization=True)
        tokenizer.save_pretrained(output_dir)
        method = 'Transformer本体＋分類ヘッド微調整（タイトル・要約、最大256トークン）'
    else:
        vectorizer, x = vectorize([docs[i] for i in train_idx])
        classifier = LogisticRegression(class_weight='balanced', max_iter=500, random_state=42).fit(x, y[train_idx])
        def predict(items):
            return classifier.predict_proba(vectorizer.transform(items))[:, 1]
        method = '文字n-gram TF-IDF＋ロジスティック回帰（Transformer未使用）'
    progress(85, '関連度と独立評価を計算しています')
    metrics = None
    if len(test_idx):
        pred = (predict([docs[i] for i in test_idx]) >= .5).astype(int)
        precision, recall, f1, _ = precision_recall_fscore_support(y[test_idx], pred, average='binary', zero_division=0)
        metrics = dict(precision=round(float(precision), 3), recall=round(float(recall), 3), f1=round(float(f1), 3), n=len(test_idx))
    scores = predict(texts(rows)).tolist()
    return {'method': method, 'metrics': metrics, 'train_count': len(train_idx), 'label_counts': dict(counts),
            'includes_agent_labels': any(r.get('label_source') == 'agent' for r in labeled),
            'evaluation_note': '少数標本の参考値。検索全体の再現率ではありません。' if metrics else '独立評価なし（件数不足、分割不可、またはエージェント判断を含む）。',
            'scores': dict(zip([r['id'] for r in rows], scores))}

def refinement_terms(rows):
    protected_full_text=[(r['title']+' '+r.get('abstract','')).casefold() for r in rows if r.get('label')=='keep']
    positive = texts([r for r in rows if r.get('label') == 'keep'])
    negative = texts([r for r in rows if r.get('label') == 'exclude'])
    if not positive or not negative:
        return {'include': [], 'exclude': []}
    # Predictions contribute candidate vocabulary, never overwrite confirmed labels.
    positive += texts([r for r in rows if not r.get('label') and r.get('score') is not None and r['score'] >= .8])
    negative += texts([r for r in rows if not r.get('label') and r.get('score') is not None and r['score'] <= .2])
    positive, negative = [t.casefold() for t in positive], [t.casefold() for t in negative]
    # Preserve Japanese compound nouns and English words instead of arbitrary subword fragments.
    v = TfidfVectorizer(token_pattern=r'(?u)[一-龯ァ-ヶー]{2,20}|[a-zA-Z][a-zA-Z0-9-]{2,30}', max_features=16000, sublinear_tf=True)
    try:
        x = v.fit_transform(positive + negative)
    except ValueError:
        return {'include': [], 'exclude': []}
    delta = np.asarray(x[:len(positive)].mean(axis=0) - x[len(positive):].mean(axis=0)).ravel()
    features = v.get_feature_names_out()
    def choose(order, source, protected):
        out = []
        for index in order:
            word = features[index].strip()
            if len(word) < 3 or re.search(r'[\s。、，,（）()]', word) or any(word in s or s in word for s in out):
                continue
            if any(word in text for text in protected):
                continue
            if any(word in text for text in source):
                out.append(word)
            if len(out) == 5:
                break
        return out
    return {'include': choose(np.argsort(-delta), positive, []), 'exclude': choose(np.argsort(delta), negative, protected_full_text)}

def demo_rows():
    # All examples are synthetic and intentionally use non-publication identifiers.
    themes = [
        ('硫化物固体電解質', '硫化物固体電解質を用いた全固体電池。電極界面の接触抵抗を低減しイオン伝導性を改善する。', 'H01M10/0562'),
        ('酸化物セラミック電池', '酸化物固体電解質を焼結した全固体電池。界面抵抗を低減する接合層と積層工程。', 'H01M10/058'),
        ('高分子電解質膜', '高分子固体電解質とリチウム塩からなる電池。柔軟な膜と電極の密着性を改善する。', 'H01M10/0565'),
        ('液系電池の冷却装置', '液系リチウム電池の冷却水路と熱交換器。車両搭載時の温度を制御する。', 'H01M10/052'),
        ('画像認識処理装置', '画像認識ニューラルネットワークによる製品外観検査。カメラ画像の欠陥を検出する。', 'G06V10/00'),
        ('半導体素子の製造', '半導体基板上の絶縁膜と配線層を形成する露光工程。トランジスタの歩留まりを向上する。', 'H01L21/00'),
    ]
    rows = []
    for t, (title, abstract, ipc) in enumerate(themes):
        for i in range(12):
            rows.append(dict(id=f'DEMO-{t+1:02d}-{i+1:03d}', title=f'{title}における{["構造","製造方法","材料設計","評価方法"][i%4]} {i+1}',
                             abstract=abstract + f' 実施例{i+1}では{["粒子径","層厚","温度","圧力"][i%4]}を調整する。',
                             applicant=['サンプル材料研究所','デモ・エナジー','仮想技術開発'][i%3], ipc=ipc, fterm='', year=str(2020+i%6), family='',
                             label=None, label_source=None, label_reason='', score=None, content_key=f'demo-{t}-{i}'))
    return rows
