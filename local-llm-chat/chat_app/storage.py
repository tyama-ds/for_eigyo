"""Small SQLite store; database files never leave this machine."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def public_attachment(item):
    return {key: item[key] for key in ('id', 'name', 'kind', 'size', 'warning') if key in item}


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / 'chat.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                sources TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'complete');
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
                payload TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')
        with self.db:
            self.db.execute("DELETE FROM attachments WHERE message_id IS NULL AND created_at < datetime('now', '-7 days')")

    def close(self):
        self.db.close()

    def settings(self):
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE id=1').fetchone()
            return json.loads(row[0]) if row else {}

    def save_settings(self, settings):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (1, ?)', (json.dumps(settings, ensure_ascii=False),))

    def conversations(self):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM conversations ORDER BY updated_at DESC')]

    def create_conversation(self, title='新しいチャット'):
        item = {'id': uuid4().hex, 'title': title, 'updated_at': now()}
        with self.lock, self.db:
            self.db.execute('INSERT INTO conversations VALUES (:id,:title,:updated_at)', item)
        return item

    def conversation(self, ident, full=False):
        with self.lock:
            row = self.db.execute('SELECT * FROM conversations WHERE id=?', (ident,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['messages'] = []
            for row in self.db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid', (ident,)):
                message = dict(row)
                message['sources'] = json.loads(message['sources'])
                attachments = [json.loads(a[0]) for a in self.db.execute('SELECT payload FROM attachments WHERE message_id=?', (row['id'],))]
                message['attachments'] = attachments if full else [public_attachment(a) for a in attachments]
                result['messages'].append(message)
            return result

    def delete_conversation(self, ident):
        with self.lock, self.db:
            self.db.execute('DELETE FROM conversations WHERE id=?', (ident,))

    def add_attachment(self, payload):
        item = {**payload, 'id': uuid4().hex}
        with self.lock, self.db:
            self.db.execute('INSERT INTO attachments VALUES (?,NULL,?,?)', (item['id'], json.dumps(item, ensure_ascii=False), now()))
        return public_attachment(item)

    def pending_attachments(self, identifiers):
        result = []
        with self.lock:
            for ident in identifiers:
                row = self.db.execute('SELECT payload FROM attachments WHERE id=? AND message_id IS NULL', (ident,)).fetchone()
                if not row:
                    raise ValueError('添付ファイルが見つからないか、すでに送信されています。もう一度添付してください。')
                result.append(json.loads(row[0]))
        return result

    def add_message(self, conversation_id, role, content, attachments=(), sources=(), status='complete'):
        ident = uuid4().hex
        stamp = now()
        with self.lock, self.db:
            self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                            (ident, conversation_id, role, content, stamp, json.dumps(list(sources), ensure_ascii=False), status))
            for attachment in attachments:
                changed = self.db.execute('UPDATE attachments SET message_id=? WHERE id=? AND message_id IS NULL', (ident, attachment['id'])).rowcount
                if not changed:
                    raise ValueError('添付ファイルはすでに送信されています。')
            self.db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (stamp, conversation_id))
            if role == 'user':
                title = (content.strip() or ('添付: ' + attachments[0]['name'] if attachments else '新しいチャット'))[:45]
                self.db.execute("UPDATE conversations SET title=? WHERE id=? AND title='新しいチャット'", (title, conversation_id))
        return ident
