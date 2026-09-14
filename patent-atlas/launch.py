"""Open Patent Atlas on this PC, starting its local server when necessary."""

import ctypes
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8810/"
# Loopback traffic must not go through the corporate web proxy.
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def server_ready():
    try:
        with HTTP.open(URL + "openapi.json", timeout=2) as response:
            schema = json.load(response)
        return schema.get("info", {}).get("title") == "Patent Atlas"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def launch():
    # Serialize rapid double-clicks so that they cannot start duplicate servers.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, "Local\\PatentAtlasLauncher8810")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(mutex, 90000) in (0, 0x80)
        if not acquired:
            raise RuntimeError("別の起動処理が続いています。少し待ってから開き直してください。")
        if not server_ready():
            with socket.socket() as probe:
                probe.settimeout(2)
                if probe.connect_ex(("127.0.0.1", 8810)) == 0:
                    raise RuntimeError("ポート8810を別のアプリが使用しているか、起動処理中です。少し待ってから開き直してください。")
            python = ROOT / ".venv" / "Scripts" / "python.exe"
            if not python.is_file():
                raise RuntimeError("Python環境が見つかりません。アプリのフォルダーで start.ps1 を実行してください。")
            with (ROOT / "server.out.log").open("ab") as stdout, (ROOT / "server.err.log").open("ab") as stderr:
                process = subprocess.Popen(
                    [str(python), "run.py", "--port", "8810"],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            deadline = time.monotonic() + 60
            while not server_ready():
                if process.poll() is not None:
                    raise RuntimeError("サーバーを起動できませんでした。")
                if time.monotonic() >= deadline:
                    raise RuntimeError("起動に時間がかかっています。少し待ってから開き直してください。")
                time.sleep(0.5)
        if not webbrowser.open(URL, new=2):
            raise RuntimeError(f"ブラウザーを開けませんでした。次のURLを開いてください。\n{URL}")
    finally:
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        launch()
    except Exception as error:
        message = f"{error}\n\nアプリのフォルダー:\n{ROOT}\n\n起動ログ: server.err.log"
        ctypes.windll.user32.MessageBoxW(None, message, "Patent Atlas — 起動", 0x10)
