"""相談の音声入力: ブラウザで録音した音声（WAV）を Whisper 互換の文字起こし API へ中継する。

対応する接続先:
  - OpenAI 互換 /v1/audio/transcriptions … faster-whisper-server（speaches）・LocalAI・vLLM・OpenAI 等
  - whisper.cpp の server（URL が /inference で終わるとき。multipart の形が少し違う）
  - Azure OpenAI（/openai/deployments/<デプロイ名>/audio/transcriptions?api-version=）

音声には質問の内容（社外秘の話も含みうる）が入るので、接続先が「クラウド」のときは設定の
「音声をクラウドへ送ってよい」を ON にしない限り送らない（ローカル LLM と同じ考え方）。
"""
from __future__ import annotations

import io
import json
import math
import socket
import struct
import time
import urllib.error
import urllib.request
import uuid
import wave

MAX_AUDIO = 25 * 1024 * 1024        # OpenAI の上限に合わせる


class SpeechError(RuntimeError):
    pass


def _opener(w: dict) -> urllib.request.OpenerDirector:
    if not w.get("use_proxy"):
        handler = urllib.request.ProxyHandler({})
    elif w.get("proxy_url"):
        handler = urllib.request.ProxyHandler({"http": w["proxy_url"], "https": w["proxy_url"]})
    else:
        handler = urllib.request.ProxyHandler()
    return urllib.request.build_opener(handler)


def target(cfg: dict) -> tuple[str, dict, str]:
    """(URL, ヘッダ, 形式 "openai" | "whisper.cpp")。"""
    w = cfg["speech"]["whisper"]
    url = (w.get("url") or "").strip().rstrip("/")
    if not url:
        raise SpeechError("文字起こし（Whisper）の接続先が未設定です（設定タブ → 音声入力）")
    key = w.get("api_key") or ""
    if w.get("provider") == "azure":
        if "/audio/transcriptions" not in url:
            url = (f"{url}/openai/deployments/{w.get('model') or 'whisper'}/audio/transcriptions"
                   f"?api-version={w.get('api_version') or '2024-06-01'}")
        return url, ({"api-key": key} if key else {}), "openai"
    if url.endswith("/inference"):
        return url, ({"Authorization": f"Bearer {key}"} if key else {}), "whisper.cpp"
    if not url.endswith("/audio/transcriptions"):
        url += "/audio/transcriptions"
    return url, ({"Authorization": f"Bearer {key}"} if key else {}), "openai"


def check_allowed(cfg: dict) -> None:
    sp = cfg["speech"]
    if sp["whisper"].get("location") == "cloud" and not sp.get("allow_cloud"):
        raise SpeechError("文字起こしの接続先が「クラウド」です。音声を外部へ送るには、設定の"
                          "「音声をクラウドへ送ってよい」を ON にしてください")


def _multipart(fields: dict[str, str], audio: bytes, filename: str, ctype: str) -> tuple[bytes, str]:
    boundary = "----docvault" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
                  .encode("utf-8"))
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
              f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
    out.write(audio)
    out.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def transcribe(cfg: dict, audio: bytes, *, content_type: str = "audio/wav", language: str = "") -> dict:
    """音声 → {"text", "seconds", "url"}。失敗は SpeechError。"""
    if not audio:
        raise SpeechError("音声が空です")
    if len(audio) > MAX_AUDIO:
        raise SpeechError(f"音声が長すぎます（上限 {MAX_AUDIO // 1024 // 1024}MB）")
    check_allowed(cfg)
    w = cfg["speech"]["whisper"]
    url, headers, kind = target(cfg)
    lang = (language or cfg["speech"].get("language") or "ja").split("-")[0]
    fields = {"response_format": "json", "temperature": "0", "language": lang}
    if kind == "openai" and w.get("provider") != "azure":
        fields["model"] = w.get("model") or "whisper-1"
    ext = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm", "audio/ogg": "ogg",
           "audio/mpeg": "mp3", "audio/mp4": "m4a"}.get(content_type.split(";")[0].strip(), "wav")
    body, ctype = _multipart(fields, audio, f"question.{ext}", content_type.split(";")[0] or "audio/wav")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype, **headers})
    started = time.time()
    try:
        with _opener(w).open(req, timeout=float(w.get("timeout") or 120)) as res:
            raw = res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        raise SpeechError(f"文字起こし API エラー HTTP {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise SpeechError(f"文字起こし API に接続できません: {e}") from e
    try:
        data = json.loads(raw)
        text = str(data.get("text") or "") if isinstance(data, dict) else ""
    except ValueError:
        text = raw                                  # response_format を無視して文字列で返す実装
    return {"text": text.strip(), "seconds": round(time.time() - started, 1), "url": url}


def test_tone() -> bytes:
    """接続テスト用の 1 秒の音（16kHz・モノラル・WAV）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
                          for i in range(16000))
        wf.writeframes(frames)
    return buf.getvalue()
