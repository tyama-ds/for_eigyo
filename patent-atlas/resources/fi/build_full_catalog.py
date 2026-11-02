"""Build all-domain FI / Japanese IPC data from two official JPO ZIPs.

Usage: python resources/fi/build_full_catalog.py FI_XML_ZIP PARALLEL_VIEWER_ZIP
No network access is performed by this script or by application startup.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import html
import json
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'https://www.jpo.go.jp/e/system/patent/gaiyo/seido-bunrui/bulkdata.html'
XML_URL = 'https://www.jpo.go.jp/e/system/patent/gaiyo/seido-bunrui/document/bulkdata/fi_xml202605.zip'
JA_URL = 'https://www.jpo.go.jp/support/general/searchportal/cgi-bin/search-portal/narabe_tool/data_20260818.zip'


def plain(text):
    text = re.split(r'<br\s*/?>\s*<br|<b><ul>', text, flags=re.I)[0]
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', text)).split()).strip()


def symbol(value):
    if re.fullmatch(r'[A-H]\d{2}[A-Z]\d{13}[0A-Z]', value):
        base = value[:4] + str(int(value[4:8])) + '/' + value[8:14].rstrip('0').ljust(2, '0')
        return base + (',' + value[14:17] if value[14:17] != '000' else '') + ('@' + value[17] if value[17] != '0' else '')
    return value


def display_code(value):
    value = unicodedata.normalize('NFKC', value).replace('\\', '').strip()
    value = re.sub(r'(/\d+)\s+(\d{3})(?=\s|$)', r'\1,\2', value)
    value = re.sub(r'\s+', '', value)
    return re.sub(r'(?<=[0-9])([A-Z])$', r'@\1', value) if '/' in value else value


def write(path, data):
    path.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))


def build(xml_zip, parallel_zip):
    records, excluded = {}, Counter()
    with ZipFile(xml_zip) as archive:
        root = ET.fromstring(archive.read('fi_xml202605/FI_XML/fi_202604.xml'))
        def visit(node, parent=None):
            if node.tag == 'fiEntry':
                kind = node.get('kind', '')
                code = symbol(node.get('symbol', ''))
                title = node.find('textBody/title')
                valid = kind in ('s', 'c', 'u', 'm') or kind.isdigit()
                if valid and re.fullmatch(r'[A-H](?:\d{2}(?:[A-Z](?:\d{1,4}/\d{2,6}(?:,\d{3})?(?:@[A-Z])?)?)?)?', code) and title is not None:
                    level = {'s':'section','c':'class','u':'subclass','m':'main_group'}.get(kind, 'subgroup')
                    row = [code, plain(''.join(title.itertext())), parent, level, int(kind) if kind.isdigit() else 0, '', []]
                    if code in records and records[code] != row:
                        raise ValueError('Conflicting classification: ' + code)
                    records[code] = row
                    parent = code
                elif valid:
                    excluded[kind] += 1
            for child in node:
                visit(child, parent)
        visit(root)
        for name in archive.namelist():
            if '/CONCORDANCE_FItoIPC/' not in name or not name.endswith('.csv'):
                continue
            for line in archive.read(name).decode('utf-8-sig').splitlines():
                parts = line.split(',')
                if len(parts) != 2:
                    continue
                code, raw = symbol(parts[0]), parts[1]
                if code in records and re.fullmatch(r'[A-H]\d{2}[A-Z]\d{10}', raw):
                    ipc = raw[:4] + str(int(raw[4:8])) + '/' + raw[8:].rstrip('0').ljust(2, '0')
                    if ipc not in records[code][6]: records[code][6].append(ipc)
    with gzip.open(ROOT/'resources/ipc/ipc_2026_01_en.json.gz', 'rt', encoding='utf-8') as f:
        valid_ipc = {row[0] for row in json.load(f)['records']}
    captions = {}
    with ZipFile(parallel_zip) as archive:
        for name in archive.namelist():
            is_fi = '/data_fi/' in name
            if not name.endswith('.txt') or not (is_fi or '/data_ipc/' in name): continue
            for line in archive.read(name).decode('euc_jp').splitlines():
                fields = line.split('\t')
                if len(fields) < 4: continue
                code = display_code(fields[0])
                caption = plain(fields[6] if is_fi and '/' in code and len(fields) >= 8 else fields[2])
                if not caption: continue
                if is_fi and code in records:
                    records[code][5] = caption
                elif not is_fi and code in valid_ipc:
                    captions[code] = dict(code=code, title_ja=caption, title_ja_status='official_translation',
                                         title_ja_source=JA_URL, title_ja_version='2026-08-18 snapshot')
    sources = [dict(url=url, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
               for path, url in ((xml_zip, XML_URL), (parallel_zip, JA_URL))]
    metadata = dict(kind='FI', version='2026.04', count=len(records), complete=not excluded,
                    coverage=list('ABCDEFGH'), japanese_count=sum(bool(r[5]) for r in records.values()),
                    publisher='JPO', source=SOURCE, sources=sources, excluded_symbols=dict(excluded),
                    limitations=['FIの階層・英語名称は2026.04、日本語名称は2026-08-18公開データ。',
                                 '分類の存在と技術への適合性は別です。対応IPCは対照表の候補で、公報への付与を意味しません。'])
    write(ROOT/'resources/fi/fi_202604.json.gz', dict(metadata=metadata, records=list(records.values())))
    (ROOT/'resources/fi/metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    ja_meta = dict(count=len(captions), complete=len(captions)==len(valid_ipc), source=JA_URL,
                   version='2026-08-18 snapshot', publisher='JPO', sources=sources[1:],
                   limitations=['現行IPCコードと照合した公式日本語名称。英語2026.01との文言の完全一致は保証しません。'])
    write(ROOT/'resources/ipc/ipc_ja_jpo.json.gz', dict(metadata=ja_meta, records=captions))
    print(json.dumps(dict(fi=metadata, ipc_japanese=ja_meta), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('xml_zip', type=Path)
    parser.add_argument('parallel_zip', type=Path)
    args = parser.parse_args()
    build(args.xml_zip, args.parallel_zip)
