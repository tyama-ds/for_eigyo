import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .attachments import extract_attachment
from .context import build_context
from . import network
from .storage import Store

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 20 * 1024 * 1024


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = 'http://127.0.0.1:1234/v1'
    model: str = Field(default='', max_length=250)
    system_prompt: str = Field(default='日本語で、わかりやすく回答してください。', max_length=10000)
    temperature: float = Field(default=.7, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=64, le=32768)
    context_chars: int = Field(default=60000, ge=8000, le=500000)
    vision_enabled: bool = False
    proxy_mode: Literal['direct', 'environment', 'manual'] = 'direct'
    proxy_url: str = Field(default='', max_length=2000)
    ca_bundle: str = Field(default='', max_length=1000)

    @field_validator('base_url')
    @classmethod
    def valid_base(cls, value):
        value = value.strip().rstrip('/')
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError('API URL の形式を確認してください。')
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('API URL は認証情報・クエリを含まない http(s) の URL を指定してください。')
        if len(value) > 2000:
            raise ValueError('API URL が長すぎます。')
        return value

    @field_validator('proxy_url')
    @classmethod
    def valid_proxy(cls, value):
        value = value.strip()
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError('プロキシ URL の形式を確認してください。')
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('プロキシは http(s)://ユーザー名@ホスト:ポート 形式で指定し、パスワードは専用欄に入力してください。')
        return value

    @field_validator('ca_bundle')
    @classmethod
    def valid_ca(cls, value):
        value = value.strip()
        if value and (not Path(value).is_absolute() or not Path(value).is_file()):
            raise ValueError('CA証明書は、このPCにある PEM ファイルの絶対パスを指定してください。')
        return value


class SettingsUpdate(Settings):
    api_key: str | None = Field(default=None, max_length=4000)
    proxy_password: str | None = Field(default=None, max_length=2000)
    api_key_clear: bool = False
    proxy_password_clear: bool = False


class ConversationInput(BaseModel):
    title: str = Field(default='新しいチャット', min_length=1, max_length=100)


class WebInput(BaseModel):
    url: str = Field(min_length=1, max_length=4000)


class ChatInput(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100)
    message: str = Field(default='', max_length=20000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    web_enabled: bool = False


def sse(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


def create_app(data_dir=None):
    directory = Path(data_dir or os.environ.get('LOCAL_CHAT_DATA_DIR', ROOT.parent / 'data'))

    @asynccontextmanager
    async def lifespan(app):
        yield
        app.state.store.close()

    app = FastAPI(title='Local LLM Chat', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = Store(directory)
    app.state.secrets = {'api_key': os.environ.get('LOCAL_CHAT_API_KEY', ''), 'proxy_password': os.environ.get('LOCAL_CHAT_PROXY_PASSWORD', '')}
    app.state.active = set()

    @app.middleware('http')
    async def local_only(request, call_next):
        host = request.url.hostname
        if host not in ('127.0.0.1', 'localhost', '::1'):
            return JSONResponse({'detail': 'このアプリは localhost から利用してください。'}, status_code=403)
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{request.headers.get("host", "")}'
        if origin and origin != expected:
            return JSONResponse({'detail': '別のサイトからの操作は許可されていません。'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '別のサイトからの操作は許可されていません。'}, status_code=403)
        if (request.method not in ('GET', 'HEAD', 'OPTIONS') or request.url.path == '/api/models') and request.headers.get('x-local-chat') != '1':
            return JSONResponse({'detail': 'アプリの画面から操作してください。'}, status_code=403)
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'リクエスト形式が不正です。'}, status_code=400)
        if length > MAX_UPLOAD + 1024 * 1024:
            return JSONResponse({'detail': '1ファイルは20 MBまでです。'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Do not echo input values, especially credentials, in validation errors.
        messages = []
        for error in exc.errors():
            messages.append(str(error.get('msg', '入力値を確認してください。')).removeprefix('Value error, '))
        return JSONResponse({'detail': '\n'.join(messages)}, status_code=422)

    def config():
        # Stored values were validated on save. A moved CA file must not block
        # loading settings; the network layer reports it when used.
        return {**Settings().model_dump(), **app.state.store.settings(), **app.state.secrets}

    def public_config():
        value = config()
        for secret in ('api_key', 'proxy_password'):
            value[secret + '_set'] = bool(value.pop(secret))
        return value

    def conversation_or_404(ident, full=False):
        value = app.state.store.conversation(ident, full)
        if value is None:
            raise HTTPException(404, 'チャットが見つかりません。')
        return value

    @app.get('/api/settings')
    async def get_settings():
        return public_config()

    @app.put('/api/settings')
    async def update_settings(body: SettingsUpdate):
        fields = body.model_dump()
        if fields['proxy_mode'] == 'manual' and not fields['proxy_url']:
            raise HTTPException(422, '手動プロキシの URL を指定してください。')
        for secret in ('api_key', 'proxy_password'):
            value = fields.pop(secret)
            clear = fields.pop(secret + '_clear')
            if clear:
                app.state.secrets[secret] = ''
            elif value is not None:
                app.state.secrets[secret] = value
        app.state.store.save_settings(fields)
        return public_config()

    @app.get('/api/models')
    async def models():
        try:
            return {'models': await network.list_models(config())}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(502, str(exc))

    @app.get('/api/conversations')
    async def conversations():
        return app.state.store.conversations()

    @app.post('/api/conversations')
    async def new_conversation(body: ConversationInput):
        return app.state.store.create_conversation(body.title)

    @app.get('/api/conversations/{ident}')
    async def get_conversation(ident: str):
        return conversation_or_404(ident)

    @app.delete('/api/conversations/{ident}')
    async def delete_conversation(ident: str):
        if ident in app.state.active:
            raise HTTPException(409, '回答を停止してからチャットを削除してください。')
        conversation_or_404(ident)
        app.state.store.delete_conversation(ident)
        return {'ok': True}

    @app.get('/api/conversations/{ident}/export')
    async def export(ident: str):
        value = conversation_or_404(ident)
        lines = ['# ' + value['title'], '']
        for message in value['messages']:
            lines += ['## ' + ('あなた' if message['role'] == 'user' else 'アシスタント'), '']
            if message['reasoning']:
                lines += ['### 思考過程', '', message['reasoning'], '', '### 回答', '']
            lines += [message['content'], '']
            for attachment in message['attachments']:
                lines += ['添付: ' + attachment['name'], '']
            for source in message['sources']:
                lines += ['参照: ' + source.get('url', ''), '']
            if message['status'] != 'complete':
                lines += ['状態: ' + message['status'], '']
        return Response('\n'.join(lines), media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="chat-{ident}.md"'})

    @app.post('/api/attachments')
    async def upload(file: UploadFile = File(...)):
        try:
            raw = await file.read(MAX_UPLOAD + 1)
            if len(raw) > MAX_UPLOAD:
                raise HTTPException(413, '1ファイルは20 MBまでです。')
            payload = await run_in_threadpool(extract_attachment, file.filename or 'attachment', raw)
            return app.state.store.add_attachment(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        finally:
            await file.close()

    @app.post('/api/web')
    async def web(body: WebInput):
        try:
            page = await network.fetch_web(config(), body.url)
            return app.state.store.add_attachment({'name': page['title'] or page['url'], 'kind': 'text', 'size': len(page['text'].encode('utf-8')), 'text': '参照URL: ' + page['url'] + '\n' + page['text'], 'warning': page.get('warning', '')})
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, str(exc))

    @app.post('/api/chat')
    async def chat(body: ChatInput, request: Request):
        settings = config()
        if not settings['model'].strip():
            raise HTTPException(422, '設定で接続先とモデル名を指定してください。')
        if not body.message.strip() and not body.attachment_ids:
            raise HTTPException(422, 'メッセージを入力するかファイルを添付してください。')
        if len(set(body.attachment_ids)) != len(body.attachment_ids):
            raise HTTPException(422, '同じ添付ファイルが重複しています。')
        if body.conversation_id in app.state.active:
            raise HTTPException(409, 'このチャットは回答中です。完了または停止を待ってください。')
        value = conversation_or_404(body.conversation_id, full=True)
        try:
            attachments = app.state.store.pending_attachments(body.attachment_ids)
            messages, warnings = build_context(settings, value['messages'], body.message, attachments)
            user_id = app.state.store.add_message(body.conversation_id, 'user', body.message, attachments)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        app.state.active.add(body.conversation_id)

        async def generate():
            output, reasoning, sources = [], [], []
            status = 'interrupted'
            try:
                yield sse('meta', {'conversation_id': body.conversation_id, 'user_message_id': user_id})
                for warning in warnings:
                    yield sse('warning', {'message': warning})
                async for event in network.stream_chat(settings, messages, body.web_enabled):
                    if await request.is_disconnected():
                        break
                    if event['event'] == 'delta':
                        output.append(event['data']['content'])
                    elif event['event'] == 'reasoning':
                        reasoning.append(event['data']['content'])
                    elif event['event'] == 'source':
                        sources.append(event['data'])
                    yield sse(event['event'], event['data'])
                else:
                    status = 'complete'
            except asyncio.CancelledError:
                raise
            except (ValueError, RuntimeError) as exc:
                status = 'error'
                yield sse('error', {'message': str(exc)})
            except Exception:
                status = 'error'
                yield sse('error', {'message': '回答の受信中にエラーが発生しました。接続設定とサーバーの状態を確認してください。'})
            finally:
                try:
                    assistant_id = app.state.store.add_message(body.conversation_id, 'assistant', ''.join(output),
                                                               sources=sources, status=status, reasoning=''.join(reasoning))
                finally:
                    app.state.active.discard(body.conversation_id)
            yield sse('done', {'message_id': assistant_id, 'status': status})

        return StreamingResponse(generate(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/health')
    async def health():
        return {'ok': True, 'app': 'local-llm-chat'}

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'static' / 'index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app
