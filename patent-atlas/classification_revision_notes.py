"""Small, source-verified IPC revision notes available without network access.

This is not an exhaustive revision concordance table. A missing note says
nothing about whether a symbol is valid, and a note never replaces a symbol.
Only formatting is normalized; incomplete codes and other classification
systems must be handled by the caller before looking up an IPC note.
"""
from __future__ import annotations

from copy import deepcopy

from classification_catalog import normalize_code


_NOTES = {
    "G05D1/02": {
        "code": "G05D1/02",
        "status": "retired",
        "last_verified_version": "2023.01",
        "changed_version": "2024.01",
        "explanation": (
            "IPC 2023.01では「二次元における位置または進路の制御」の正式分類でした。"
            "2024.01の改訂で削除され、複数の分類へ再編されています。"
            "1対1の置換ではないため、技術内容に応じて改訂対応表を確認してください。"
            "旧文献の分類記載として現れる場合があります。"
        ),
        "sources": [
            {
                "title": "WIPO IPC 2023.01 G05D 分類表（1ページ）",
                "url": (
                    "https://www.wipo.int/ipc/itos4ipc/ITSupport_and_download_area/"
                    "20230101/pdf/scheme/full_ipc/en/g05d.pdf"
                ),
            },
            {
                "title": "WIPO IPC 2024.01 改訂対応表（G05D 1/02）",
                "url": (
                    "https://ipcpub.wipo.int/?notion=rcl&symbol=G05D0001020000"
                    "&version=20240101&lang=en"
                ),
            },
            {
                "title": "WIPO IPC 2024.01 G05D 分類表",
                "url": (
                    "https://www.wipo.int/ipc/itos4ipc/ITSupport_and_download_area/"
                    "20240101/pdf/scheme/full_ipc/en/g05d.pdf"
                ),
            },
        ],
    },
}


def lookup(code: str) -> dict | None:
    """Return an independent verified IPC note, without choosing a replacement."""
    if not isinstance(code, str) or not code:
        return None
    note = _NOTES.get(normalize_code(code))
    return deepcopy(note) if note is not None else None
