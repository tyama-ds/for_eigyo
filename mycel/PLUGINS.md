# Mycel プラグインの作り方

プラグインは `mycel/plugins/<id>.py` に置く Python ファイルです。中に `Plugin` のサブクラスを
1 つ定義し、設定画面の「プラグイン」で有効にすると読み込まれます（設定の `plugins` 配列に `<id>` が入る）。
プラグイン内の例外は本体に波及しません（ログに出るだけ）。

```python
from mycelcore.plugins import Plugin, PluginVeto


class Example(Plugin):
    name = "例"
    description = "設定画面に出る説明"

    def setup(self, ctx):
        super().setup(ctx)                     # self.ctx が使えるようになる
        self.dir = ctx.data_dir("example")     # <vault>/.mycel/plugins/example/

    def before_save(self, event):              # 保存の直前。止めたいときは PluginVeto
        if "社外秘" in (event.text or "") and event.path.startswith("共有/"):
            raise PluginVeto("共有フォルダに社外秘は置けません")

    def on_saved(self, event):                 # 保存の直後
        self.ctx.log(self.id, f"{event.author} が {event.path} を保存（版 {event.version}）")

    def routes(self):                          # /api/plugins/example/<名前>
        return {"hello": lambda params: {"message": "こんにちは"}}

    def status(self):                          # 設定画面に出す状態
        return {"state": "ok"}
```

## フック

| メソッド | 呼ばれるとき | 使いみち |
|----------|--------------|----------|
| `setup(ctx)` / `teardown()` | 読み込み / 無効化・終了 | 初期化・後片付け |
| `on_vault_opened()` | Vault を開いたとき | 共有先からの取り込み |
| `before_save(event)` | 作成・保存の直前 | ロック・権限・内容チェック（`PluginVeto` で中止、画面にメッセージ） |
| `on_created(event)` | 作成の直後 | 通知・同期 |
| `on_saved(event)` | 保存の直後（名前変更に伴うリンク書き換えも含む） | 同期・履歴 |
| `on_deleted(event)` | 削除の直後 | 同期 |
| `on_renamed(event)` | 名前変更の直後（`event.old_path` に旧パス） | 同期 |
| `routes()` | API 呼び出し時 | `GET /api/plugins/<id>/<名前>`（クエリ）・`POST`（JSON 本文）で独自 API。`params["_method"]` に `GET` / `POST` が入る。**状態を変える処理は POST のときだけ行う**（POST だけが CSRF 対策の対象） |
| `status()` | 設定画面を開いたとき | 状態表示 |

`event`（`NoteEvent`）の中身: `kind`（created / saved / deleted / renamed）、`path`、`old_path`、
`author`（設定の作成者名）、`version`（内容ハッシュ）、`text`（本文。削除では `None`）、`timestamp`。

`ctx`（`PluginContext`）: `ctx.vault`（読み書き）、`ctx.index`（検索・リンク）、`ctx.config()`、
`ctx.data_dir(id)`、`ctx.log(id, msg)`。

Vault の外部で .md を書き換えたときは `ctx.index.refresh(相対パス)` を呼ぶとすぐ反映されます
（呼ばなくても数秒以内に差分同期されます）。

## 共有に進むとき

`plugins/shared_vault.py` がひな形です。次の順で肉付けする想定です。

1. **置き場所を決める**: 社内ファイルサーバ上の共有フォルダ / Git リモート / 専用サーバ。
   いちばん手軽なのは `git_history` に `pull` / `push` を足す方法
2. **送り出し**: `on_saved` などで変更を送る。`change_journal` の journal.jsonl を未送信の台帳に使える
3. **取り込み**: `on_vault_opened` と定期タイマーで取り込み、`ctx.vault.write()` → `ctx.index.refresh()`
4. **競合**: 本体の保存 API は楽観ロック（`base_version`）なので、取り込みで版が変わると
   編集中の画面は 409 を受けて「相手の版 / 自分の版」を選ばせる。自動マージが必要ならプラグインで行う
5. **ロック・権限**: `before_save` で `PluginVeto("○○さんが編集中です")`。作成者は `event.author`
6. **公開範囲**: 現在サーバは `127.0.0.1` のみに bind。複数人が 1 台のサーバを使う形にする場合は、
   認証を入れてから bind 先を変えること（今の API には認証がありません）
