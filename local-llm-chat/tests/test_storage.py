import sqlite3

from chat_app.storage import Store


def test_legacy_database_migrates_assistant_reasoning_once(tmp_path):
    # Reproduce the previous released schema, including old interrupted output.
    db = sqlite3.connect(tmp_path / 'chat.sqlite3')
    db.executescript('''
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
            sources TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'complete');
        INSERT INTO conversations VALUES ('chat', '旧会話', '2026-10-09');
    ''')
    messages = [
        ('user', 'user', '<think>このタグについて教えて</think>', 'complete'),
        ('answer', 'assistant', '<think>順に考える。</think>回答です。', 'complete'),
        ('code', 'assistant', '例: `<think>sample</think>`', 'complete'),
        ('partial', 'assistant', '<think>まだ考え中', 'interrupted'),
    ]
    db.executemany('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)',
                   [(ident, 'chat', role, content, '2026-10-09', '[]', status)
                    for ident, role, content, status in messages])
    db.commit()
    db.close()

    store = Store(tmp_path)
    saved = store.conversation('chat')['messages']
    assert [(m['content'], m['reasoning']) for m in saved] == [
        ('<think>このタグについて教えて</think>', ''),
        ('回答です。', '順に考える。'),
        ('例: `<think>sample</think>`', ''),
        ('', 'まだ考え中'),
    ]
    assert saved[-1]['status'] == 'interrupted'
    store.add_message('chat', 'assistant', '新しい回答', reasoning='新しい思考')
    snapshot = store.conversation('chat')
    store.close()

    reopened = Store(tmp_path)
    assert reopened.conversation('chat') == snapshot
    reopened.close()
