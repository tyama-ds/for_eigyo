"""Import prior search strategies without flattening their Boolean meaning.

Editable imports deliberately cover Patent Atlas portable JSON and the explicit
J-PlatPat subset /TX, /IP, /CP, /FT, [...], +, *, binary -. Mixed operators at the
same bracket level need explicit grouping: this module does not guess precedence.
All other expressions remain useful, immutable references. No input is executed.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from query_formats import _class, _literal, build_tree

MAX_INPUT = 100_000
MAX_NODES = 256
MAX_DEPTH = 12
FORMATS = {
    'jplatpat': 'J-PlatPat', 'derwent_innovation': 'Derwent Innovation',
    'derwent_dii': 'Derwent Innovations Index', 'espacenet': 'Espacenet',
    'uspto': 'USPTO', 'patentscope': 'PATENTSCOPE',
    'google_patents': 'Google Patents', 'portable_json': 'Patent Atlas JSON',
    'reference': 'その他・不明',
}


def _string(value, label, limit=500, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f'{label}は{limit:,}文字以内の文字列で入力してください。')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError(f'{label}に制御文字を含められません。')
    return value.strip()


def _node_dict(node):
    if node.op == 'text':
        return {'op': 'text', 'value': node.value}
    if node.op == 'class':
        return {'op': 'class', 'value': node.value, 'system': node.system}
    return {'op': node.op, 'children': [_node_dict(c) for c in node.children]}


def validate_tree(tree):
    """Return only canonical, bounded data; never trust imported extra fields."""
    count = 0

    def visit(node, depth):
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError('検索式が複雑すぎます。256条件・12階層以内に分けてください。')
        if not isinstance(node, dict):
            raise ValueError('検索条件の木構造が不正です。')
        op = node.get('op')
        if op in ('text', 'class'):
            if node.get('children'):
                raise ValueError('語句・分類の条件に子条件を指定できません。')
            value = _string(node.get('value'), '検索条件', empty=False)
            if op == 'text':
                if any(c in value for c in '\r\n\t'):
                    raise ValueError('検索語句に改行やタブを含められません。')
                return {'op': 'text', 'value': _literal(value)}
            return _node_dict(_class(value, node.get('system')))
        if op not in ('and', 'or', 'not'):
            raise ValueError('未対応の論理演算子です。AND・OR・二項のNOTで記述してください。')
        children = node.get('children')
        if not isinstance(children, list) or not 2 <= len(children) <= 64:
            raise ValueError('論理演算子の子条件は2件以上64件以内で指定してください。')
        if op == 'not' and len(children) != 2:
            raise ValueError('NOTは「対象条件 AND NOT 除外条件」の2件で指定してください。')
        return {'op': op, 'children': [visit(c, depth + 1) for c in children]}

    return visit(tree, 0)


def _parse_jplatpat(text):
    tokens, start, quote = [], 0, None
    for i, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ('\"', "'"):
            quote = char
        elif char in '[]+*-':
            if text[start:i].strip():
                tokens.append(text[start:i].strip())
            tokens.append(char)
            start = i + 1
    if quote:
        raise ValueError('引用符が閉じていません。英文の句は全体を引用符で囲んでください。')
    if text[start:].strip():
        tokens.append(text[start:].strip())
    if len(tokens) > MAX_NODES * 5:
        raise ValueError('検索式が複雑すぎます。短い式に分けてください。')
    index = 0

    def atom(depth):
        nonlocal index
        if depth > MAX_DEPTH:
            raise ValueError('検索式は12階層以内に分けてください。')
        if index >= len(tokens):
            raise ValueError('演算子の後に条件がありません。')
        token = tokens[index]
        index += 1
        if token == '[':
            node = sequence(depth + 1)
            if index >= len(tokens) or tokens[index] != ']':
                raise ValueError('角括弧が閉じていません。')
            index += 1
            return node
        if token in (']', '+', '*', '-'):
            raise ValueError('空の条件や単項NOTには対応していません。')
        match = re.fullmatch(r'(.+)/(TX|IP|CP|FT)', token, re.I | re.S)
        if not match:
            raise ValueError('対応項目は /TX・/IP・/CP・/FT です。各語句・分類に項目を付けてください。')
        value, field = match[1].strip(), match[2].upper()
        if field == 'TX':
            if value[:1] in ('\"', "'"):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError('検索語句の引用符を確認してください。')
                value = value[1:-1]
            elif re.search(r'\s', value):
                raise ValueError('空白を含む句は引用符で囲んでください。単語への分割は行いません。')
            return {'op': 'text', 'value': value}
        return _node_dict(_class(value, {'IP': 'IPC', 'CP': 'CPC', 'FT': 'F-term'}[field]))

    def sequence(depth):
        nonlocal index
        nodes, operators = [atom(depth)], []
        while index < len(tokens) and tokens[index] != ']':
            operator = tokens[index]
            index += 1
            if operator not in ('+', '*', '-'):
                raise ValueError('条件の間に +・*・- のいずれかを指定してください。')
            operators.append(operator)
            nodes.append(atom(depth))
        if not operators:
            return nodes[0]
        if len(set(operators)) != 1 or (operators[0] == '-' and len(operators) != 1):
            raise ValueError('同じ階層の異なる演算子は優先順位を推測しません。[...]でAND・OR・NOTの範囲を明示してください。')
        return {'op': {'+': 'or', '*': 'and', '-': 'not'}[operators[0]], 'children': nodes}

    if not tokens:
        raise ValueError('検索式を入力してください。')
    tree = sequence(0)
    if index != len(tokens):
        raise ValueError('余分な閉じ括弧があります。')
    return validate_tree(tree)


def _query_from_tree(tree):
    terms, classes, positive, negative = [], [], [], []

    def walk(node, excluded=False):
        if node['op'] == 'text':
            if node['value'] not in terms:
                terms.append(node['value'])
            target = negative if excluded else positive
            if node['value'] not in target:
                target.append(node['value'])
        elif node['op'] == 'class':
            classification = {'kind': node['system'], 'code': node['value'], 'verified': False}
            if classification not in classes:
                classes.append(classification)
        else:
            for i, child in enumerate(node['children']):
                walk(child, not excluded if node['op'] == 'not' and i == 1 else excluded)

    walk(tree)
    # These lists are summaries for editors. boolean_tree is the authority for
    # export; converting these lists back into an AND query would lose meaning.
    return {
        'boolean_tree': tree, 'keywords': positive, 'include_terms': [],
        'exclude_terms': negative, 'classifications': classes,
        'type': '検索式例からの調整',
    }, terms, [f"{c['kind']}:{c['code']}" for c in classes]


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSONの同じキーが重複しています: ' + key)
        result[key] = value
    return result


def _facets(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError('観点は30件以内の文字列で指定してください。')
    return [_string(item, '観点', 500, empty=False) for item in value]


def parse_example(text, source_format='jplatpat', name='', purpose=''):
    """Parse one paste/file; unsupported syntax becomes a reference-only entry."""
    original_text = text
    text = _string(text, '検索式またはJSON', MAX_INPUT, empty=False)
    source_format = _string(source_format, '元の検索サービス', 80, empty=False)
    name = _string(name, '検索式例の名前', 160)
    purpose = _string(purpose, '元の検索目的', 4000)
    entry = {
        'id': str(uuid4()), 'name': name or '名称未設定の検索式例',
        'purpose': purpose, 'facets': [], 'source_format': source_format,
        'original_text': original_text, 'status': 'reference_only', 'issues': [],
        'query': None, 'terms': [], 'classification_terms': [],
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    is_json = source_format == 'portable_json' or text.lstrip().startswith('{')
    data = None
    if is_json:
        try:
            data = json.loads(text, object_pairs_hook=_unique_json,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限の数値は指定できません。')))
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('JSONを読み込めません。形式と括弧を確認してください。') from exc
    try:
        if is_json:
            if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1 or not isinstance(data.get('query'), dict):
                raise ValueError('Patent Atlas JSONは schema_version: 1 と query を持つオブジェクトで指定してください。')
            query = data['query']
            entry['name'] = name or _string(data.get('name', ''), '検索式例の名前', 160) or entry['name']
            entry['purpose'] = purpose or _string(data.get('purpose', ''), '元の検索目的', 4000)
            entry['facets'] = _facets(data.get('facets'))
            entry['source_format'] = _string(data.get('source_format', 'portable_json'), '元の検索サービス', 80, empty=False)
            if 'boolean_tree' in query:
                tree = validate_tree(query['boolean_tree'])
            else:
                # The existing structured snapshot describes AND keywords,
                # OR classifications, OR inclusions, then AND NOT exclusions.
                if not any(query.get(k) for k in ('keywords', 'classifications')):
                    raise ValueError('JSONに構造化した条件がありません。expressionだけの式は参照として保持します。')
                tree = validate_tree(_node_dict(build_tree(query)))
        elif source_format == 'jplatpat':
            tree = _parse_jplatpat(text)
        else:
            raise ValueError('このサービスの式は参照として保存します。調整するには、J-PlatPatの対応構文またはPatent Atlas JSONで条件を登録してください。')
        entry['query'], entry['terms'], entry['classification_terms'] = _query_from_tree(tree)
        entry['status'] = 'editable'
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError) as exc:
        entry['issues'] = [str(exc) or '式の構造を確認できません。参照として保存します。']
    return entry


def _replacement_map(value, allowed, label):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_NODES:
        raise ValueError(f'{label}は元の条件と新しい条件の組み合わせで指定してください。')
    result = {}
    for key, replacement in value.items():
        if key not in allowed:
            raise ValueError(f'{label}の置換元が検索式例にありません: {str(key)[:160]}')
        result[key] = _string(replacement, label, 500, empty=False)
    return result


def adapt_example(entry, replacements=None, class_replacements=None):
    """Create a detached preview. Classification systems and Boolean scopes stay."""
    if not isinstance(entry, dict) or entry.get('status') != 'editable' or not isinstance(entry.get('query'), dict):
        raise ValueError('この検索式例は参照専用です。対応構文か構造化JSONで再登録してください。')
    tree = validate_tree(entry['query'].get('boolean_tree'))
    _, terms, classes = _query_from_tree(tree)
    replacements = _replacement_map(replacements, terms, '語句の置換')
    class_replacements = _replacement_map(class_replacements, classes, '分類の置換')

    def change(node):
        node = deepcopy(node)
        if node['op'] == 'text' and node['value'] in replacements:
            node['value'] = replacements[node['value']]
        elif node['op'] == 'class':
            key = node['system'] + ':' + node['value']
            if key in class_replacements:
                node = _node_dict(_class(class_replacements[key], node['system']))
        elif 'children' in node:
            node['children'] = [change(child) for child in node['children']]
        return node

    query, _, _ = _query_from_tree(validate_tree(change(tree)))
    query['provenance'] = {
        'example_id': entry.get('id'), 'example_name': entry.get('name', ''),
        'source_format': entry.get('source_format', ''),
        'original_purpose': entry.get('purpose', ''), 'facets': deepcopy(entry.get('facets', [])),
        'replacements': replacements, 'class_replacements': class_replacements,
    }
    return query


def portable_example(entry):
    """A plain-data export that a user can edit and re-import on another PC."""
    if not isinstance(entry, dict) or entry.get('status') != 'editable':
        raise ValueError('参照専用の例は構造化JSONに変換できません。元の式をコピーしてください。')
    query, _, _ = _query_from_tree(validate_tree(entry['query']['boolean_tree']))
    return {'schema_version': 1, 'name': entry.get('name', ''),
            'purpose': entry.get('purpose', ''), 'facets': deepcopy(entry.get('facets', [])),
            'source_format': entry.get('source_format', ''), 'query': query}
