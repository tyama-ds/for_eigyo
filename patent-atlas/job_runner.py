"""Single background job per workspace: start, report progress, request a stop.

Every function receives the Workspace explicitly; the job and stop event live
in ``ws.job`` and ``ws.stop``. Terminal status is published only after the
workspace commit so a poll never sees "done" before the data is durable.
"""

import threading
import time
import uuid

from fastapi import HTTPException


def progress(ws, value, message):
    with ws.lock:
        ws.job.update(progress=value, message=message)
        ws.invalidate_public_state()


def request_stop(ws):
    ws.stop.set()
    with ws.lock:
        ws.job["stop_requested"] = True
        ws.invalidate_public_state()
    return {"message": "停止要求を送信しました。実行中のAPI応答・モデル読込後に停止します。"}


def begin_job(ws, action, *, kind=None, mode=None, save=None):
    """Run ``action`` on a daemon thread; ``save`` keeps the replaceable seam."""
    save = ws.save if save is None else save
    with ws.lock:
        ws.idle()
        ws.stop.clear()
        ws.job.update(
            id=uuid.uuid4().hex,
            status="running",
            progress=0,
            message="準備中",
            error=None,
            kind=kind,
            mode=mode,
            started_at=time.time(),
            completed_at=None,
            stop_requested=False,
        )
        ws.invalidate_public_state()

    def worker():
        try:
            action()
            with ws.lock:
                # Publish terminal job status only after the workspace commit.
                save()
                if ws.stop.is_set():
                    ws.job.update(status="cancelled", message="停止しました")
                else:
                    ws.job.update(status="done", progress=100, message="完了しました")
                ws.job["completed_at"] = time.time()
                ws.invalidate_public_state()
        except Exception as exc:
            with ws.lock:
                try:
                    save()
                except Exception:
                    exc = ValueError("保存に失敗しました。保存先の空き容量と書き込み権限を確認してください。")
                ws.job.update(
                    status="cancelled" if ws.stop.is_set() else "error",
                    error=str(exc)
                    if isinstance(exc, (ValueError, HTTPException))
                    else "処理に失敗しました。設定・入力データ・依存パッケージを確認してください。",
                    message="処理を終了しました",
                )
                ws.job["completed_at"] = time.time()
                ws.invalidate_public_state()

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": ws.job["id"]}
