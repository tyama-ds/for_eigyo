"""Source-backed, conservative patent-search exporters.

Compile structured conditions, never rewrite the displayed expression with regex.
Unsupported classifications fail closed unless their omission is explicitly requested.
Reference verification is not a claim that a live database executed the query.
"""
from dataclasses import dataclass
import re

VERIFIED_AT = '2026-09-12'
PROFILES = {
    'jplatpat': dict(name='J-PlatPat', interface='特許・実用新案検索 → 論理式入力', url='https://www.j-platpat.inpit.go.jp/p0100',
        systems=['IPC','CPC','F-term'], text_scope='全文（/TX）。和文・英文の検索対象を検索画面で指定。',
        class_note='IPCは /IP、CPCは /CP、F-termは /FT。通常の分類検索は下位階層も含みます。「指定コード」は完全一致の意味ではありません。上位IPCはサブクラスの論理式出力に対応。',
        sources=[('J-PlatPat 論理式入力ヘルプ','https://www.j-platpat.inpit.go.jp/help/ja/p01/arithmetic.html'),
                 ('特許庁 GXTI検索式のJ-PlatPat入力例（H02S/IP）','https://www.jpo.go.jp/resources/statistics/document/gxti/gxti_jplatpat.pdf')], language='ja_en', descendants=False),
    'derwent_innovation': dict(name='Derwent Patent Search / Innovation', interface='Expert Search（専門検索）', url='https://www.derwentinnovation.com/',
        systems=['IPC','CPC','F-term'], text_scope='タイトル・要約・請求項（CTB）。コレクションによってDWPIも検索。明細書の全文TEXTとは範囲が異なります。',
        class_note='IC=IPCすべて、ACP=CPCすべて、FTC=F-term。上位IPCのクラス・サブクラスはワイルドカードなしで指定。2020年の公式公開研修資料に基づく構文です。現契約画面のCheck Syntaxで確認してください。',
        sources=[('Clarivate 分類検索研修（2020）','https://clarivate.com/intellectual-property/wp-content/uploads/sites/5/dlm_uploads/200924_DIsearch_basic3_presentation.pdf'),
                 ('Clarivate キーワード検索研修（2020）','https://clarivate.com/intellectual-property/wp-content/uploads/sites/5/dlm_uploads/200910_DIsearch_basic1_presentation.pdf')], language='en', descendants=False),
    'derwent_dii': dict(name='Derwent Innovations Index（Web of Science）', interface='Advanced Search / Query Builder', url='https://www.webofscience.com/',
        systems=['IPC'], text_scope='Topic（TS）：Derwentレコードのタイトル・要約。特許全文ではありません。',
        class_note='IPCは IP。上位IPCのクラス・サブクラスは末尾 * で枝全体を検索。DIIの CPC は被引用出願人コードであり、CPC分類ではありません。',
        sources=[('Clarivate DII Field Tags','https://webofscience.zendesk.com/hc/en-us/articles/25550079343121-Derwent-Advanced-Search-Field-Tags'),
                 ('Clarivate DII 分類検索とワイルドカード','https://webofscience.zendesk.com/hc/en-us/articles/20139602979985-Derwent-Document-Search-Fields'),
                 ('Clarivate 検索規則・前方一致の制約','https://webofscience.zendesk.com/hc/en-us/articles/25350084904721-Search-Rules'),
                 ('Clarivate DII Topic search','https://images.webofknowledge.com/WOKRS58B4_1/help/DII/hs_topic.html'),
                 ('国立国会図書館 DIIのIPC入力形式','https://ndlsearch.ndl.go.jp/rnavi/stm/post_381')], language='en', descendants=False),
    'espacenet': dict(name='Espacenet（EPO / 欧州）', interface='Smart search', url='https://worldwide.espacenet.com/',
        systems=['IPC','CPC'], text_scope='全文（ftxt）：タイトル・要約・明細書・請求項。',
        class_note='ipc= と cpc= を分けて出力。上位IPCは自動的に下位分類も検索します。細分類の下位指定は /low。分類に * を使いません。',
        sources=[('EPO Espacenet pocket guide','https://link.epo.org/web/technical/espacenet/espacenet-pocket-guide-en.pdf'),
                 ('EPO 下位分類 /low','https://worldwide.espacenet.com/patent/help/query-syntax-low-operator'),
                 ('EPO 検索言語','https://worldwide.espacenet.com/patent/help/query-languages')], language='en_de_fr', descendants=True),
    'uspto': dict(name='USPTO Patent Public Search（米国）', interface='Advanced Search → Search pane', url='https://ppubs.uspto.gov/pubwebapp/',
        systems=['IPC','CPC'], text_scope='全文（フィールド無指定）。複合語は順序付き ADJ で連結。',
        class_note='細分類は .IPC. / .CPC.。上位IPCは $ を付けて .CIPC.（International Classification）で接頭一致検索。フィールドと収録年代によって検索対象が異なります。細分類の階層展開は行いません。',
        sources=[('USPTO Advanced search guide','https://www.uspto.gov/sites/default/files/documents/Advanced-search-overview-QRG-Patent-Public-Search.pdf'),
                 ('USPTO Searchable indexes','https://www.uspto.gov/patents/search/patent-public-search/searchable-indexes'),
                 ('USPTO Operators','https://www.uspto.gov/patents/search/patent-public-search/operators'),
                 ('USPTO Efficient searching・展開語数の上限','https://www.uspto.gov/patents/search/patent-public-search/efficient-searching')], language='en', descendants=False),
    'patentscope': dict(name='WIPO PATENTSCOPE', interface='Advanced Search', url='https://patentscope.wipo.int/search/en/advancedSearch.jsf',
        systems=['IPC','CPC'], text_scope='言語別の全文（EN_ALLTXT / JA_ALLTXT）。',
        class_note='細分類の指定コードは IC_EX / CPC_EX、下位展開は IC / CPC。上位IPCは常に IC で下位分類を含む枝全体を検索します。',
        sources=[('WIPO Query syntax','https://patentscope.wipo.int/search/en/help/querySyntaxHelp.jsf'),
                 ('WIPO Field codes','https://patentscope.wipo.int/search/en/help/fieldsHelp.jsf'),
                 ('WIPO 日本語ユーザーガイド','https://patentscope.wipo.int/search/help/ja/users_guide.pdf'),
                 ('WIPO User Guide 2024（分類検索43頁・ICの上位入力例74頁）','https://patentscope2.wipo.int/search/help/en/users_guide.pdf')], language='ja_en', descendants=True),
    'google_patents': dict(name='Google Patents', interface='Advanced Search → Search Terms（ホーム画面ではありません）', url='https://patents.google.com/advanced',
        systems=['CPC'], text_scope='全文：タイトル・要約・請求項・明細書。',
        class_note='公式資料で確認したCPC構文のみ出力。IPCをCPCへ読み替えません。指定コードは CPC=、下位分類は /low。',
        sources=[('Google Patents Searching','https://support.google.com/faqs/answer/7049475?hl=en-AU')], language='any', descendants=True),
}

@dataclass(frozen=True)
class Node:
    op: str
    value: str = ''
    system: str = ''
    children: tuple = ()

def group(op, children):
    children = tuple(children)
    if not children:
        raise ValueError('空の論理条件は出力できません。')
    return children[0] if len(children) == 1 else Node(op, children=children)

def terms_of(query):
    return list(dict.fromkeys(query.get('keywords', []) + query.get('include_terms', []) + query.get('exclude_terms', [])))

def _strings(value, name, max_items=100):
    if not isinstance(value, list) or len(value)>max_items or any(not isinstance(t,str) or not t.strip() or len(t)>500 for t in value):
        raise ValueError(name + 'の形式を確認してください。')
    return list(dict.fromkeys(t.strip() for t in value))

def _literal(value):
    # Do not pretend punctuation/wildcards have identical escaping semantics on every DB.
    if re.search(r'''[\x00-\x1f\x7f"'\[\](){}\\/:=+*?$#^~<>|!;]''', value):
        raise ValueError('この形式変換では検索演算子・引用符等を含む語句を扱えません。出力用語句を修正してください: ' + value)
    if value.upper() in {'AND','OR','NOT','ANDNOT','ADJ','NEAR','SAME','WITH'}:
        raise ValueError('演算子だけの語句は出力できません: ' + value)
    return value

def _class(code, system):
    if not isinstance(code,str):
        raise ValueError('分類コードは文字列にしてください。')
    code=re.sub(r'\s+', '', code).upper()
    pattern=r'[A-HY]\d{2}[A-Z]\d{1,4}/\d{1,6}' if system=='CPC' else r'[A-H](?:\d{2}(?:[A-Z](?:\d{1,4}/\d{1,6})?)?)?' if system=='IPC' else r'\d[A-Z]\d{3}[A-Z]{2}\d{2}'
    if system not in ('IPC','CPC','F-term') or not re.fullmatch(pattern,code):
        raise ValueError('分類の体系またはコード形式が不正です: ' + str(system) + ' ' + code)
    return Node('class',code,system)


def _is_upper_ipc(node):
    return node.system == 'IPC' and bool(re.fullmatch(r'[A-H](?:\d{2}[A-Z]?)?', node.value))


def _upper_ipc_problem(node, format_id):
    if not _is_upper_ipc(node):
        return ''
    if format_id == 'derwent_innovation' and len(node.value) == 1:
        return ('Derwent Patent Search / Innovation の上位IPC ' + node.value +
                ' は、1文字のセクションを検索する構文を公式資料で確認できていないため出力を保留します。'
                '分類Lookupから取得するか、クラスまで絞ってください。')
    if format_id == 'jplatpat' and len(node.value) < 4:
        return ('J-PlatPat の上位IPC ' + node.value +
                ' は、セクション・クラス単位の論理式入力を公式資料で確認できていないため出力を保留します。'
                'サブクラスまで絞るか、PATENTSCOPE形式を選んでください。')
    if format_id == 'derwent_dii' and len(node.value) == 1:
        return ('Derwent Innovations Index の上位IPC ' + node.value +
                ' は、1文字の接頭辞によるワイルドカード検索の制約を確認できていないため出力を保留します。'
                'クラスまで絞るか、PATENTSCOPE形式を選んでください。')
    return ''

def build_tree(query, replacements=None, omit_systems=()):
    if query.get('type') == '改善案' and ('include_terms' not in query or 'exclude_terms' not in query):
        raise ValueError('旧形式の改善式には条件の構造が保存されていません。条件を確認して新しい式を作成してください。')
    base=_strings(query.get('keywords',[]),'キーワード')
    include=_strings(query.get('include_terms',[]),'追加語')
    exclude=_strings(query.get('exclude_terms',[]),'除外語')
    replacements={} if replacements is None else replacements
    if not isinstance(replacements,dict) or any(k not in base+include+exclude for k in replacements):
        raise ValueError('出力用語句のキーは元の式の語句を指定してください。')
    for key,value in replacements.items():
        if not isinstance(value,str) or not value.strip() or len(value)>500:
            raise ValueError('出力用語句を空欄にできません: ' + key)
    def text(word):
        return Node('text',_literal(replacements.get(word,word).strip()))
    base_nodes=[text(w) for w in base]
    classes=[]
    for c in query.get('classifications',[]):
        node=_class(c.get('code'),c.get('kind'))
        if node.system not in omit_systems and node not in classes:
            classes.append(node)
    if not base and not classes:
        raise ValueError('キーワードまたは出力可能な分類を指定してください。追加語・除外語だけでは検索式を作成できません。')
    if classes:
        base_nodes.append(group('or',classes))
    if include:
        base_nodes.append(group('or',[text(w) for w in include]))
    tree=group('and',base_nodes)
    if exclude:
        tree=Node('not',children=(tree,group('or',[text(w) for w in exclude])))
    return tree

def _render(node, format_id, language, descendants):
    if node.op=='text':
        word=node.value
        if format_id=='jplatpat':
            if language=='ja':
                if re.search(r'[\s-]',word):
                    raise ValueError('J-PlatPat和文の空白・ハイフン入り語句は自動変換できません。語句か検索言語を変更してください: '+word)
                return word+'/TX'
            return "'"+word+"'/TX"
        if format_id=='derwent_innovation':
            return 'CTB=("'+word+'")'
        if format_id=='derwent_dii':
            return 'TS=("'+word+'")'
        if format_id=='espacenet':
            return 'ftxt="'+word+'"'
        if format_id=='uspto':
            words=word.split()
            if any(not w.isalnum() or re.fullmatch(r'(?:AND|OR|NOT|XOR|ANDNOT|ADJ\d*|NEAR\d*|SAME\d*|WITH\d*)',w,re.I) for w in words):
                raise ValueError('USPTOでは記号や検索演算子を含む句の変換を保留します。ハイフンは空白に置き換えるなど、出力用語句を確認してください: '+word)
            return '('+' ADJ '.join(words)+')' if len(words)>1 else word
        if format_id=='patentscope':
            return ('JA_ALLTXT' if language=='ja' else 'EN_ALLTXT')+':("'+word+'")'
        return '"'+word+'"'
    if node.op=='class':
        code=node.value
        if _is_upper_ipc(node):
            problem = _upper_ipc_problem(node, format_id)
            if problem:
                raise ValueError(problem)
            if format_id == 'jplatpat':
                return code + '/IP'
            if format_id == 'derwent_innovation':
                # Clarivate classification training p39 gives IC=((F01)); / IC=((F01B));.
                return 'IC=(' + code + ')'
            if format_id == 'derwent_dii':
                return 'IP=(' + code + '*)'
            if format_id == 'espacenet':
                # Upper symbols are auto-posted: no wildcard or subgroup /low rewrite.
                return 'ipc=' + code
            if format_id == 'uspto':
                return code + '$.CIPC.'
            if format_id == 'patentscope':
                return 'IC:("' + code + '")'
            raise ValueError('この検索サービスでは上位IPCの出力に対応していません。')
        if format_id=='jplatpat':
            return code+{'IPC':'/IP','CPC':'/CP','F-term':'/FT'}[node.system]
        if format_id=='derwent_innovation':
            display_code=re.sub(r'^([A-HY]\d{2}[A-Z])',r'\1 ',code) if node.system!='F-term' else code
            return {'IPC':'IC','CPC':'ACP','F-term':'FTC'}[node.system]+'=('+display_code+')'
        if format_id=='derwent_dii':
            match=re.fullmatch(r'([A-H]\d{2}[A-Z])(\d+)/(\d+)',code)
            return 'IP=('+match[1]+'-'+match[2].zfill(3)+'/'+match[3]+')'
        if format_id=='espacenet':
            return node.system.lower()+'='+code+('/low' if descendants else '')
        if format_id=='uspto':
            return code+'.'+node.system+'.'
        if format_id=='patentscope':
            field='IC' if node.system=='IPC' else 'CPC'
            display_code=re.sub(r'^([A-HY]\d{2}[A-Z])',r'\1 ',code)
            return field+('' if descendants else '_EX')+':("'+display_code+'")'
        return 'CPC='+code+('/low' if descendants else '')
    children=[_render(c,format_id,language,descendants) for c in node.children]
    if format_id=='jplatpat':
        if node.op=='or':
            return '['+'+'.join(children)+']'
        if node.op=='and':
            return '*'.join('['+text+']' if child.op in ('text','class') else text for child,text in zip(node.children,children))
        right=children[1][1:-1] if node.children[1].op=='or' else children[1]
        return '['+children[0]+']-['+right+']'
    operator={'and':' AND ','or':' OR ','not':' ANDNOT ' if format_id=='patentscope' else ' NOT '}[node.op]
    return '('+operator.join(children)+')'

def formats():
    return [dict(id=k,**v,verified_at=VERIFIED_AT,verification='公式資料照合・実サイトの検索結果は未検証') for k,v in PROFILES.items()]

def export_query(query,format_id,*,replacements=None,omit_unsupported=False,allow_unverified=False,descendants=False,language='auto'):
    if format_id not in PROFILES:
        raise ValueError('出力する検索サービスを選んでください。')
    if language not in ('auto','en','ja') or any(not isinstance(v,bool) for v in (omit_unsupported,allow_unverified,descendants)):
        raise ValueError('出力オプションの形式を確認してください。')
    profile=PROFILES[format_id]
    classes=query.get('classifications',[])
    unsupported=[c for c in classes if c.get('kind') not in profile['systems']]
    unverified=[c for c in classes if not c.get('verified') and c not in unsupported]
    problems=[]
    warnings=[]
    supported_nodes = [_class(c.get('code'), c.get('kind')) for c in classes if c not in unsupported]
    upper_nodes = [node for node in supported_nodes if _is_upper_ipc(node)]
    for node in upper_nodes:
        problem = _upper_ipc_problem(node, format_id)
        if problem:
            problems.append(problem)
    if unsupported and not omit_unsupported:
        problems.append('この出力形式で扱えない分類: '+', '.join(str(c.get('kind'))+' '+str(c.get('code')) for c in unsupported))
    if unverified and not allow_unverified:
        problems.append('存在・定義が未確認の分類: '+', '.join(str(c.get('code')) for c in unverified))
    if descendants and not profile['descendants'] and any(not _is_upper_ipc(node) for node in supported_nodes):
        problems.append('このサービスの下位分類への変換は未対応です。「指定コード」に戻してください。')
    omitted=unsupported if omit_unsupported else []
    tree=build_tree(query,replacements,{c['kind'] for c in omitted})
    emitted_terms=[(replacements or {}).get(t,t).strip() for t in terms_of(query)]
    has_ja=any(re.search(r'[\u3040-\u30ff\u3400-\u9fff]',t) for t in emitted_terms)
    actual_language=('ja' if has_ja else 'en') if language=='auto' else language
    if has_ja and profile['language'] in ('en','en_de_fr'):
        problems.append('日本語の語句が残っています。下の「出力用の語句」で英語など検索先が対応する言語に置き換えてください。自動翻訳は行いません。')
    if format_id=='patentscope' and actual_language=='en' and has_ja:
        problems.append('PATENTSCOPEの英語フィールドに日本語が指定されています。言語か出力用語句を変更してください。')
    if format_id=='jplatpat' and actual_language=='en' and has_ja:
        problems.append('J-PlatPatの英文検索対象に日本語が残っています。言語か出力用語句を変更してください。')
    if omitted:
        warnings.append('今回の出力から '+', '.join(c['kind']+' '+c['code'] for c in omitted)+' を外しました。元の条件と同じ検索ではありません。')
        warnings.append('OR分類群から一部を外すと絞り込みが強まり、分類群すべてを外すと範囲が広がります。')
    if unverified and allow_unverified:
        warnings.append('未確認分類を利用者の指定で含めました。構文変換はコードの存在や意味を保証しません。')
    if upper_nodes:
        warnings.append('上位IPC（'+', '.join(dict.fromkeys(node.value for node in upper_nodes))+'）は、その下位分類を含む枝全体の条件です。「指定コード」でもこの範囲を保持し、細分類の検索条件は変えません。')
        if format_id == 'uspto':
            warnings.append('上位IPCは CIPC フィールドの接頭一致で出力しています。細分類の IPC フィールドとは対象が異なります。展開語が20,000件を超えると実行できないため、その場合は下位の分類へ絞ってください。')
    changed=[dict(original=k,output=v.strip()) for k,v in (replacements or {}).items() if v.strip()!=k]
    if changed:
        warnings.append('出力用語句を置換しています。翻訳・語句変更の同等性は確認してください。元の検索式は変更していません。')
    if format_id.startswith('derwent_'):
        warnings.append('Derwent固有レコードの検索対象です。原公報の全文検索と検索範囲が異なります。')
    if format_id=='uspto':
        warnings.append('ADJの距離ではストップワードが無視されます。句の一致条件は他DBと完全には同じではありません。')
    warnings.append('各DBの収録国・言語・分類付与・語形処理が異なるため、同じ論理条件でも結果件数は一致しません。')
    expression='' if problems else _render(tree,format_id,actual_language,descendants)
    if expression and format_id=='derwent_innovation':
        expression+=';'
    if format_id=='derwent_dii':
        warnings.append('IPCのハイフン・主群3桁表記は国立国会図書館の操作案内に基づきます。現行契約環境で構文を確認してください。')
    if format_id=='jplatpat':
        instructions='「特許・実用新案検索」→「論理式入力」の検索式欄へ貼り付けます。'
        instructions+=('テキスト検索対象は'+('和文' if actual_language=='ja' else '英文')+'を選択してください。') if emitted_terms else '今回は分類条件のみです。'
    else:
        instructions=profile['interface']+' の式入力欄に貼り付けてください。'
    return dict(format_id=format_id,name=profile['name'],query_id=query.get('id'),version=query.get('version'),expression=expression,can_copy=not problems,
                problems=problems,warnings=warnings,omitted_classifications=omitted,replacements=changed,terms=terms_of(query),
                language=actual_language,text_scope=profile['text_scope'] if emitted_terms else '分類条件のみ（本文検索条件なし）',classification_note=profile['class_note'],
                instructions=instructions,url=profile['url'],sources=[dict(title=t,url=u) for t,u in profile['sources']],
                verified_at=VERIFIED_AT,verification='公式資料照合・実サイトでの検索実行は未検証')
