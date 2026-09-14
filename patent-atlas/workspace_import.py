"""Append search results without replacing previously reviewed patents."""
import copy
import re


def publication_key(row):
    identifier = str(row['id'])
    return re.sub(r'[^A-Z0-9]', '', identifier.upper()) or 'raw:' + identifier


def merge_patents(existing, incoming):
    rows = copy.deepcopy(existing)
    indexed = {publication_key(row): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError('既存データの公報番号が重複しています。追加を中止しました。')
    report = dict(added=0, matched=0, metadata_filled=0, differing_metadata=0)
    for item in incoming:
        key = publication_key(item)
        previous = indexed.get(key)
        if previous is None:
            new = copy.deepcopy(item)
            rows.append(new)
            indexed[key] = new
            report['added'] += 1
            continue
        if (any(str(previous.get(k) or '') != str(item.get(k) or '') for k in ('title', 'abstract'))
                or previous.get('content_key') and item.get('content_key')
                and previous['content_key'] != item['content_key']):
            raise ValueError(f'{item["id"]} の名称または要約が既存データと異なります。判定を維持するため追加を中止しました。')
        report['matched'] += 1
        for field in ('ipc', 'fi', 'fterm', 'applicant', 'year', 'family'):
            if item.get(field) and not previous.get(field):
                previous[field] = copy.deepcopy(item[field])
                report['metadata_filled'] += 1
            elif item.get(field) and previous.get(field) != item[field]:
                report['differing_metadata'] += 1
    if len(rows) > 5000:
        raise ValueError('追加後の特許群は5,000件までです。追加を中止しました。')
    return rows, report
