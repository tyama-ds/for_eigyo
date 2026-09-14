"""Small, source-linked seed catalog. This is NOT a complete classification index."""
CATALOG = [
    dict(code='H01M10/0562', kind='IPC', title='無機固体電解質', group='電解質', terms=['電池','battery','固体','solid','電解質','electrolyte','硫化物','酸化物'], source='https://ipcpub.wipo.int/'),
    dict(code='H01M10/0565', kind='IPC', title='高分子電解質（ゲルを含む）', group='電解質', terms=['電池','battery','高分子','polymer','電解質','electrolyte'], source='https://ipcpub.wipo.int/'),
    dict(code='H01M10/052', kind='IPC', title='リチウム二次電池', group='電池構造', terms=['電池','battery','リチウム','lithium','全固体'], source='https://ipcpub.wipo.int/'),
    dict(code='H01M10/058', kind='IPC', title='二次電池の構造・製造', group='製造・構造', terms=['電池','battery','製造','界面','積層','構造'], source='https://ipcpub.wipo.int/'),
    dict(code='H01M4/139', kind='IPC', title='非水系二次電池用電極の製造', group='電極', terms=['電池','battery','電極','electrode','正極','負極','界面'], source='https://www.wipo.int/ipc/itos4ipc/ITSupport_and_download_area/20260101/pdf/scheme/full_ipc/en/h01m.pdf'),
    dict(code='5H029AM11', kind='F-term', title='固体電解質', group='電解質', terms=['電池','battery','固体','solid','電解質','electrolyte'], source='https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList5H029.html'),
    dict(code='5H029AM12', kind='F-term', title='アルカリ金属イオン伝導固体電解質', group='電解質', terms=['電池','battery','リチウム','固体','電解質','イオン'], source='https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList5H029.html'),
    dict(code='5H029AJ06', kind='F-term', title='内部抵抗の低減', group='性能・界面', terms=['電池','battery','抵抗','resistance','界面','interface'], source='https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList5H029.html'),
]

def keywords(text):
    import re
    if not isinstance(text,str):
        raise ValueError('キーワードは文字列で入力してください。')
    if text.count('"') % 2:
        raise ValueError('フレーズを囲む二重引用符が閉じていません。')
    out=[]
    for match in re.finditer(r'"([^"\r\n]+)"|([^\s,、;；]+)',text.strip()):
        quoted, bare=match.groups()
        if quoted is not None and match.end()<len(text.strip()) and not re.match(r'[\s,、;；]',text.strip()[match.end()]):
            raise ValueError('フレーズ同士は空白またはカンマで区切ってください。')
        term=(quoted if quoted is not None else bare).strip()
        if '"' in term or (bare and (bare.upper() in {'AND','OR','NOT','ANDNOT','NEAR','ADJ'} or re.search(r'[()\[\]{}|]',bare))):
            raise ValueError('入力欄はキーワード用です。Boolean式は入力できません。複合語は "solid electrolyte" のように二重引用符で囲んでください。')
        if not term or len(term)>500:
            raise ValueError('各キーワードは1〜500文字にしてください。')
        if term not in out:
            out.append(term)
    if len(out)>24:
        raise ValueError('キーワードは24個までです。超過分を削除せず入力を停止しました。')
    return out

def suggest(text):
    words = keywords(text)
    out = []
    for c in CATALOG:
        matches = [w for w in words if any(t.lower() in w.lower() or w.lower() in t.lower() for t in c['terms'])]
        if matches:
            out.append({**c, 'reason': '入力「' + '・'.join(matches) + '」との語句一致。分類の定義を出典で確認してください。', 'verified': True, 'score': len(matches)})
    return sorted(out, key=lambda c: -c['score'])
