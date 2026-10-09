"""Temporary local UI smoke-test fixture; never connects to a real LLM."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_live import _configure, fake_llm, live_app

if __name__ == '__main__':
    output = Path(__file__).resolve().parents[1] / 'test-results'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='local-chat-ui-') as directory:
        with contextmanager(fake_llm.__wrapped__)() as llm, contextmanager(live_app.__wrapped__)(Path(directory)) as application:
            _configure(application.client, llm)
            information = {
                'url': str(application.client.base_url), 'llm_url': llm.url, 'model': 'socket-model',
                'scenarios': ['reasoning-demo', 'reasoning-structured-demo', 'reasoning-only-demo', 'reasoning-whitespace-demo', 'slow-reasoning-test'],
            }
            (output / 'ui-fixture.json').write_text(json.dumps(information), encoding='utf-8')
            (output / 'sample.txt').write_text('UIテスト用の資料です。新製品の検討会は来週月曜日。担当は開発チーム。', encoding='utf-8')
            print(json.dumps(information), flush=True)
            stop = output / 'ui-fixture.stop'
            if stop.exists():
                stop.unlink()
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and not stop.exists():
                time.sleep(.2)
