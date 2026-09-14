"""Rebuild the offline theme snapshot from JPO's downloaded workbook (stdlib only)."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def build(source, output):
    ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(source) as archive:
        strings = [''.join(node.itertext()).replace('_x000D_', '').strip()
                   for node in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('x:si', ns)]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    records = []
    for row in sheet.findall('x:sheetData/x:row', ns):
        cells = {}
        for cell in row:
            value = cell.find('x:v', ns)
            if value is not None:
                cells[re.sub(r'\d', '', cell.get('r'))] = (strings[int(value.text)] if cell.get('t') == 's' else value.text).strip()
        if re.fullmatch(r'\d[A-Z]\d{3}', cells.get('A', '')):
            records.append(dict(code=cells['A'], deactivated=bool(cells.get('B')), analysis_type=cells.get('C', ''),
                                title=cells.get('D', ''), fi_range=cells.get('E', ''), maintenance=cells.get('F', ''),
                                has_fterms=bool(cells.get('G')), start_year=cells.get('H', ''), end_year=cells.get('I', ''),
                                under_reanalysis=bool(cells.get('J'))))
    if len({r['code'] for r in records}) != len(records) or len(records) < 2000:
        raise ValueError('Unexpected theme coverage; inspect the source workbook before rebuilding.')
    data = dict(metadata=dict(source='https://www.jpo.go.jp/system/patent/gaiyo/bunrui/fi/document/themecode/code.xlsx',
                              source_page='https://www.jpo.go.jp/system/patent/gaiyo/bunrui/fi/themecode.html',
                              source_title='特許庁 全テーマ分のテーマコード表', version='2026-06',
                              retrieved_at=datetime.now(timezone.utc).isoformat(), source_sha256=sha256(source.read_bytes()).hexdigest(),
                              scope='all_themes_in_source', theme_count=len(records),
                              verification_scope='テーマコード・FIカバー範囲の公式表。個々のFタームコードやIPCとの厳密な対応は確認しません。'),
                themes=records)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(records)} themes saved to {output}')


if __name__ == '__main__':
    directory = Path(__file__).resolve().parent
    build(directory / 'theme-code-2026-06.xlsx', directory / 'themes_2026_06.json')
