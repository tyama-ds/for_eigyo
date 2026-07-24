"""Secrets at rest — Fernet暗号化。

master keyはDB・リポジトリとは別の場所 (secret file または環境変数) から読む。
平文secretはAPI応答・SSE・ログへ出さない。参照はsecret id / secret nameのみ。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import SecretItem


class MasterKeyError(RuntimeError):
    pass


def load_master_key(settings: Settings) -> bytes:
    """master keyを取得する。優先順: secret file > 環境変数。"""
    if settings.master_key_file.is_file():
        raw = settings.master_key_file.read_text().strip()
    elif settings.master_key:
        raw = settings.master_key.strip()
    else:
        raise MasterKeyError(
            "master keyが設定されていません。DRO_MASTER_KEY_FILE のファイルを作成するか "
            "DRO_MASTER_KEY を設定してください (scripts/gen_master_key.sh 参照)。"
        )
    # 任意文字列を許容し、SHA-256でFernet鍵へ導出する
    digest = hashlib.sha256(raw.encode()).digest()
    return base64.urlsafe_b64encode(digest)


class SecretStore:
    def __init__(self, session: Session, settings: Settings):
        self._session = session
        self._fernet = Fernet(load_master_key(settings))

    def put(self, name: str, value: str) -> str:
        """secretを保存し、secret idを返す。既存nameは上書き。"""
        ciphertext = self._fernet.encrypt(value.encode())
        item = self._session.scalar(select(SecretItem).where(SecretItem.name == name))
        if item is None:
            item = SecretItem(name=name, ciphertext=ciphertext)
            self._session.add(item)
        else:
            item.ciphertext = ciphertext
        self._session.flush()
        return item.id

    def reveal(self, secret_id: str) -> str:
        """平文を復号する。呼び出し側はログ・応答へ出さないこと。"""
        item = self._session.get(SecretItem, secret_id)
        if item is None:
            raise KeyError(f"secret {secret_id} not found")
        return self._fernet.decrypt(item.ciphertext).decode()

    def delete(self, secret_id: str) -> None:
        item = self._session.get(SecretItem, secret_id)
        if item is not None:
            self._session.delete(item)
            self._session.flush()
