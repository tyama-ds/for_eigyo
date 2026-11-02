"""Rebuild the bounded EV FI snapshot from published JPO/INPIT tables.

Run manually; no network access is performed by app startup or candidate search.
FI symbols and parent paths are read independently of the WIPO IPC catalogue.
"""
from datetime import date
import gzip
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fi_catalog import PATTERN, DATA, normalize

BASE = 'https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/common/FI/ja/202604/'
GROUPS = ['B60L50/00', 'B60L53/00', 'B60L58/00', 'B60L15/00', 'B60L7/00', 'B60K1/00']


class Table(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.code = None; self.capture = False; self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr': self.code = None
        if tag == 'a' and re.fullmatch(PATTERN, attrs.get('name', '')):
            self.code = normalize(attrs['name'])
        if tag == 'td':
            self.capture = attrs.get('class') in ('explanationArea', 'explanationListArea'); self.parts = []

    def handle_data(self, text):
        if self.capture: self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == 'td' and self.capture:
            text = ' '.join(''.join(self.parts).split())
            if self.code and text: self.rows.append((self.code, text))
            self.capture = False


def build():
    records, sources = {}, []
    stamp = date.today().isoformat()
    with httpx.Client(timeout=25, follow_redirects=False) as client:
        for group in GROUPS:
            url = BASE + 'fiList/fiList' + group.replace('/', '_') + '.html'
            response = client.get(url); response.raise_for_status()
            table = Table(); table.feed(response.content.decode('utf-8-sig'))
            if not table.rows or table.rows[0][0] != group:
                raise ValueError('Unexpected FI table: ' + group)
            stack = []
            for code, caption in table.rows:
                dots = len(caption) - len(caption.lstrip('・'))
                title = caption.lstrip('・').strip()
                base = re.split('[,@]', code)[0]
                if code != base:
                    parent = base if base in records else None
                else:
                    while stack and stack[-1][0] >= dots: stack.pop()
                    parent = stack[-1][1] if stack else None
                    stack.append((dots, code))
                ancestors = records[parent]['ancestors'] + [parent] if parent else []
                records[code] = dict(kind='FI', code=code, title=title, title_ja=title,
                    title_ja_status='official', title_ja_source=url, title_ja_version='2026.04',
                    title_ja_parent=records[parent]['title'] if parent else '',
                    parent=parent, ancestors=ancestors, dots=dots,
                    level='main_group' if code==group else 'subgroup', language='ja',
                    path_title=' > '.join([records[a]['title'] for a in ancestors]+[title]),
                    verified=True, version='2026.04', source=url)
            sources.append(dict(url=url, sha256=hashlib.sha256(response.content).hexdigest(), retrieved_at=stamp))
            print(group, len(table.rows), flush=True)
    for row in records.values():
        row['children_count'] = sum(r['parent']==row['code'] for r in records.values())
    metadata = dict(kind='FI', version='2026.04', retrieved_at=stamp, count=len(records), complete=False,
        coverage=GROUPS, publisher='JPO / INPIT', sources=sources,
        limitations=['電動車両関連6メイングループの部分収録。FI全分類ではありません。',
                    'IPCと同じ記号でも別の分類体系です。未収録FIの不存在を意味しません。'])
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_bytes(gzip.compress(json.dumps(dict(metadata=metadata, records=records), ensure_ascii=False).encode('utf-8'), mtime=0))
    DATA.with_name('metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__': build()
