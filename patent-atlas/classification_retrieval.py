"""Topic-preserving dictionary retrieval, independent of model-generated codes.

Profiles are reviewed search entry points, not claims of exhaustive coverage.
Every IPC/FI is looked up separately; no code is fabricated by translation.
"""
import re
import unicodedata
import classification_catalog as ipc
import fi_catalog as fi

EV_ALIASES = ['電気自動車', '電気車両', '電動車両', '電動自動車', 'electric vehicle',
              'electric vehicles', 'electric car', 'electric cars', 'battery electric vehicle',
              'electrically propelled vehicles', 'EV', 'BEV']
EV_ENTRIES = [
    ('B60L50/60', '電動駆動', 'device', '車載電池を動力源にした電気的推進'),
    ('B60L53/00', '充電・電池交換', 'application', '車両向けの充電方式・設備・電池交換'),
    ('B60L58/10', '電池の監視・制御', 'performance', '車載電池の状態監視や制御'),
    ('B60L15/20', '駆動制御', 'device', '車両や駆動モータの速度・トルク制御'),
    ('B60L7/10', '回生制動', 'performance', '制動時の電気エネルギー回生'),
    ('B60K1/04', '車載配置', 'device', '推進用の蓄電手段を車両に配置する構造'),
    ('B60L55/00', '電力網との連携', 'application', '車両から電力網へ給電するV2G'),
]


def phrases(text):
    """Keep an English technical phrase intact instead of OR-ing its words."""
    text = unicodedata.normalize('NFKC', str(text))
    parts = re.findall(r'"([^"]+)"|([^",、;；\n]+)', text)
    return list(dict.fromkeys(t.strip() for pair in parts for t in pair
                             if t.strip().strip('"')))[:24]


def ev_topic(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold().replace('-', ' ')
    return any((alias.casefold() in text if re.search(r'[^\x00-\x7f]', alias) else
                re.search(r'(?<![a-z0-9])' + re.escape(alias.casefold()) + r'(?![a-z0-9])', text))
               for alias in EV_ALIASES)


def english_topics(text):
    return ['electric vehicle', 'electrically propelled vehicle'] if ev_topic(text) else []


def relevant_seed(text, row):
    # The legacy compact battery catalogue must not infer solid electrolytes
    # solely from "battery" within a vehicle theme.
    if not ev_topic(text): return True
    normalized = unicodedata.normalize('NFKC', text).casefold()
    return any(t.casefold() in normalized for t in row.get('terms', [])
               if t.casefold() not in {'電池', 'battery', '構造'})


def retrieve(text, limit=24):
    result = {}
    def add(row, reason, facet='device', group='名称一致', method='dictionary_title'):
        if not row: return
        row = dict(row, reason=reason, facets=[facet], group=group, recommendation_method=method,
                   terms=phrases(text), evidence_status='hypothesis', relevance_status='review_pending')
        result.setdefault(row['kind']+':'+row['code'], row)
    if ev_topic(text):
        for code, label, facet, scope in EV_ENTRIES:
            reason = f'「電気自動車」の観点「{label}」: {scope}。初期探索の入口です。対象範囲は定義と公報で確認してください。'
            add(ipc.lookup('IPC', code), reason, facet, label, 'reviewed_topic_profile')
            add(fi.lookup(code), reason, facet, label, 'reviewed_topic_profile')
        # The reviewed entry points take precedence over noisy word co-occurrence.
        return list(result.values())[:limit]
    for phrase in phrases(text):
        for row in ipc.search('IPC', phrase, limit=6):
            add(row, f'公式分類名称と「{phrase}」の語句一致。適合性は未確認です。')
        for row in fi.search(phrase, limit=3):
            add(row, f'収録FI名称と「{phrase}」の語句一致。適合性は未確認です。')
    return list(result.values())[:limit]
