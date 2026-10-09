import json

import pytest
from fastapi.testclient import TestClient

from chat_app.main import create_app
from chat_app import network

HEADERS = {'X-Local-Chat': '1'}


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8765', headers=HEADERS) as client:
        yield client


def configured(client):
    config = client.get('/api/settings').json()
    config.pop('api_key_set')
    config.pop('proxy_password_set')
    config['model'] = 'test-model'
    assert client.put('/api/settings', json=config).status_code == 200
    return config


def new_chat(client):
    return client.post('/api/conversations', json={}).json()['id']


def test_local_origin_and_header_protection(client):
    assert client.post('/api/conversations', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/settings', headers={'Host': 'evil.example'}).status_code == 403
    assert client.post('/api/conversations', json={}, headers={'X-Local-Chat': ''}).status_code == 403
    assert client.get('/api/settings', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_secrets_never_returned_or_persisted(client, tmp_path):
    config = configured(client)
    config.update(api_key='secret-api-value', proxy_password='secret-proxy-value')
    result = client.put('/api/settings', json=config)
    assert result.status_code == 200
    assert result.json()['api_key_set'] is True
    assert 'secret-api-value' not in result.text
    assert 'secret-proxy-value' not in json.dumps(client.app.state.store.settings())
    client.put('/api/settings', json={**config, 'api_key_clear': True})
    assert not client.get('/api/settings').json()['api_key_set']


def test_reject_password_in_proxy_url(client):
    config = configured(client)
    config['proxy_url'] = 'http://user:do-not-echo@proxy.example:8080'
    result = client.put('/api/settings', json=config)
    assert result.status_code == 422
    assert 'do-not-echo' not in result.text


def test_chat_stream_attachment_history_export_and_cascade(client, monkeypatch):
    configured(client)
    ident = new_chat(client)
    file = client.post('/api/attachments', files={'file': ('memo.txt', '確認用の資料'.encode(), 'text/plain')}).json()
    captured = []

    async def stream(settings, messages, web_enabled):
        captured.append((messages, web_enabled))
        yield {'event': 'delta', 'data': {'content': '了解しました。'}}
        yield {'event': 'source', 'data': {'url': 'https://example.com/', 'title': '参考'}}

    monkeypatch.setattr(network, 'stream_chat', stream)
    result = client.post('/api/chat', json={'conversation_id': ident, 'message': '要約してください', 'attachment_ids': [file['id']]})
    assert result.status_code == 200
    assert 'event: done' in result.text
    assert '確認用の資料' in captured[0][0][-1]['content']
    history = client.get('/api/conversations/' + ident).json()['messages']
    assert history[1]['content'] == '了解しました。'
    assert history[0]['attachments'][0]['name'] == 'memo.txt'
    assert 'text' not in history[0]['attachments'][0]
    assert '要約してください' in client.get(f'/api/conversations/{ident}/export').text
    repeat = client.post('/api/chat', json={'conversation_id': ident, 'message': '再送', 'attachment_ids': [file['id']]})
    assert repeat.status_code == 422
    assert client.delete('/api/conversations/' + ident).status_code == 200
    assert client.app.state.store.db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0] == 0


def test_error_saves_partial_answer(client, monkeypatch):
    configured(client)
    ident = new_chat(client)

    async def stream(*args):
        yield {'event': 'delta', 'data': {'content': '途中の回答'}}
        raise RuntimeError('通信が途切れました。')

    monkeypatch.setattr(network, 'stream_chat', stream)
    result = client.post('/api/chat', json={'conversation_id': ident, 'message': 'テスト'})
    assert 'event: error' in result.text
    saved = client.get('/api/conversations/' + ident).json()['messages'][-1]
    assert saved['content'] == '途中の回答'
    assert saved['status'] == 'error'
    assert not client.app.state.active


def test_image_requires_vision_and_no_message_saved(client):
    configured(client)
    ident = new_chat(client)
    attachment = client.app.state.store.add_attachment({'name': 'pic.png', 'kind': 'image', 'size': 1, 'data_url': 'data:image/png;base64,AA=='})
    result = client.post('/api/chat', json={'conversation_id': ident, 'message': '画像', 'attachment_ids': [attachment['id']]})
    assert result.status_code == 422
    assert client.get('/api/conversations/' + ident).json()['messages'] == []


def test_manual_web_page_attachment(client, monkeypatch):
    async def fetch(settings, url):
        return {'title': '資料', 'url': url, 'text': '本文'}
    monkeypatch.setattr(network, 'fetch_web', fetch)
    result = client.post('/api/web', json={'url': 'https://example.com/'})
    assert result.status_code == 200
    attachment = client.app.state.store.pending_attachments([result.json()['id']])[0]
    assert 'https://example.com/' in attachment['text']


def test_context_prunes_old_history_with_explicit_warning():
    from chat_app.context import build_context
    settings = {'context_chars': 8000, 'system_prompt': '', 'vision_enabled': False}
    history = []
    for i in range(10):
        history += [{'role': 'user', 'content': str(i) * 700, 'attachments': [], 'status': 'complete'},
                    {'role': 'assistant', 'content': 'a' * 700, 'attachments': [], 'status': 'complete'}]
    messages, warnings = build_context(settings, history, '今の質問', [])
    assert messages[-1]['content'] == '今の質問'
    assert len(messages) < 22
    assert any('古い会話' in warning for warning in warnings)
    assert sum(len(m['content']) for m in messages) <= 8000


def test_context_still_works_after_reducing_limit():
    from chat_app.context import build_context
    settings = {'context_chars': 8000, 'system_prompt': '', 'vision_enabled': False}
    history = [{'role': 'user', 'content': 'x' * 18000, 'attachments': [], 'status': 'complete'}]
    messages, warnings = build_context(settings, history, '次の質問', [])
    assert len(messages) == 2
    assert any('古い会話' in warning for warning in warnings)


def test_missing_saved_ca_does_not_block_settings_repair(client):
    client.app.state.store.save_settings({'ca_bundle': 'C:/no-longer-exists.pem'})
    assert client.get('/api/settings').status_code == 200
    assert client.put('/api/settings', json={'ca_bundle': ''}).status_code == 200
