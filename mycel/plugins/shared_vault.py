"""共有 Vault（将来用のひな形・未実装）。

チームで Vault を共有するときに実装を入れる場所。いまは何もせず、状態に
「未実装」と表示するだけ。既定では無効。

実装方針のメモ（PLUGINS.md の「共有に進むとき」も参照）:

1. 取り込み（pull）
   ``on_vault_opened`` と定期タイマーで共有先（社内ファイルサーバ・Git リモート・
   専用サーバ）から変更を取り込み、``ctx.vault.write()`` → ``ctx.index.refresh()`` する。

2. 送り出し（push）
   ``on_saved`` / ``on_created`` / ``on_deleted`` / ``on_renamed`` で変更を送る。
   ``change_journal`` の journal.jsonl を「未送信の変更」の台帳として使える。

3. 競合
   本体の保存 API は楽観ロック（``base_version``）で、先に誰かが書き換えていれば
   409 を返して UI が「相手の版／自分の版」を選ばせる。取り込み時も同じ版比較を使う。

4. ロック・権限
   ``before_save`` で ``PluginVeto("○○さんが編集中です")`` を投げれば保存を止められる。
   作成者はイベントの ``author``（設定の作成者名）で分かる。
"""
from __future__ import annotations

from mycelcore.plugins import Plugin


class SharedVault(Plugin):
    name = "共有 Vault（未実装）"
    description = "チーム共有・同期のためのひな形です。有効にしても現時点では何もしません"

    def status(self) -> dict:
        return {"state": "未実装", "remote": ""}
