"""Rebuild a bounded Japanese PMGS caption snapshot from a supplied code list.

Usage: python resources/ipc/build_japanese_catalog.py --codes path/to/codes.json
The list contains IPC symbols only. This helper is not run by app startup.
It uses public published classification tables, not patent result scraping.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
import classification_catalog as catalog
from classification_translations import BASE, BUNDLE, caption_records, parse_table, table_path


def build(codes):
    nodes = {}
    for code in codes:
        node = catalog.lookup('IPC', code)
        if not node:
            raise ValueError('Unknown IPC code: ' + code)
        for item in [node, *catalog.ancestors('IPC', code), *catalog.children('IPC', code)]:
            nodes[item['code']] = item
    for node in catalog.children('IPC'):
        nodes[node['code']] = node
        for item in catalog.children('IPC', node['code']):
            nodes[item['code']] = item
    paths = list(dict.fromkeys(table_path(node) for node in nodes.values()))
    if len(paths) > 100:
        raise ValueError('This helper limits a snapshot to 100 public tables.')
    records, sources = {}, []
    stamp = datetime.now(timezone.utc).date().isoformat()
    with httpx.Client(timeout=15, follow_redirects=False, headers={'User-Agent': 'PatentAtlas-ClassificationCaptions/1.0'}) as client:
        for index, path in enumerate(paths):
            response = client.get(BASE + path)
            response.raise_for_status()
            if len(response.content) > 4 * 1024 * 1024:
                raise ValueError('Oversized public table: ' + path)
            rows = parse_table(response.content.decode('utf-8-sig'))
            if not rows:
                raise ValueError('No named IPC captions: ' + path)
            records.update(caption_records(rows, path, catalog._catalog()[1], stamp))
            sources.append({'url': BASE + path, 'sha256': hashlib.sha256(response.content).hexdigest(),
                            'retrieved_at': stamp, 'parsed_rows': len(rows)})
            print(f'{index+1}/{len(paths)} {path}: {len(rows)}', flush=True)
            time.sleep(.25)
    missing = sorted(set(codes) - set(records))
    if missing:
        raise ValueError('Requested codes missing: ' + ', '.join(missing))
    metadata = {
        'kind': 'IPC', 'language': 'ja', 'publisher': 'JPO / INPIT J-PlatPat PMGS',
        'count': len(records), 'complete': False, 'version': 'PMGS snapshot ' + stamp,
        'symbol_validation_version': 'WIPO 2026.01', 'retrieved_at': stamp,
        'coverage': 'Requested symbols, their directly browsable tables, and all eight sections/classes',
        'translation_status': 'Official Japanese publication snapshot; wording edition is not asserted to match WIPO 2026.01',
        'limitations': ['This is a bounded subset, not the complete Japanese IPC catalogue.',
                       'Publication notes/illustrations and historical wording require checking linked PMGS pages.',
                       'Japanese subgroup captions depend on parent definitions; parent hierarchy remains the WIPO hierarchy.'],
        'copyright': 'Underlying IPC classification: WIPO; Japanese translation: Government of Japan.',
        'source_information': 'https://www.jpo.go.jp/system/patent/gaiyo/bunrui/ipc/ipc8wk.html',
        'sources': sources,
    }
    data = json.dumps({'metadata': metadata, 'records': records}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    BUNDLE.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    BUNDLE.with_name('metadata_ja.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'count': len(records), 'tables': len(paths), 'compressed_bytes': BUNDLE.stat().st_size}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codes', type=Path, required=True)
    args = parser.parse_args()
    build(json.loads(args.codes.read_text(encoding='utf-8')))
