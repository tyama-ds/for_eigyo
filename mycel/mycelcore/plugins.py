"""プラグインの仕組み。

``mycel/plugins/<id>.py`` に ``Plugin`` のサブクラスを 1 つ置き、設定の ``plugins`` に
``<id>`` を並べると読み込まれる。詳しくは ``PLUGINS.md``。

現状は個人利用が前提だが、将来の共有（チーム Vault・同期・ロック）をプラグインとして
後付けできるよう、次の差し込み口を用意している。

- ``before_save``   保存の直前。``PluginVeto`` を投げると保存を止められる（ロック・権限）
- ``on_saved`` / ``on_created`` / ``on_deleted`` / ``on_renamed``  変更の直後（通知・同期・履歴）
- ``on_vault_opened``  Vault を開いたとき（リモートからの取り込みなど）
- ``routes``        ``/api/plugins/<id>/<name>`` に独自 API を生やす（同期状態の表示など）
- ``status``        設定画面に出す状態

イベントには必ず ``author``（設定の作成者名）と ``version``（内容ハッシュ）が入る。
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


class PluginVeto(Exception):
    """before_save から投げると保存を中止し、メッセージを UI に表示する。"""


@dataclass
class NoteEvent:
    kind: str                       # created / saved / deleted / renamed
    path: str
    author: str
    old_path: str = ""
    version: str = ""
    text: str | None = None         # deleted では None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("text", None)
        return d


class PluginContext:
    """プラグインに渡す窓口。Vault・インデックス・設定を参照できる。"""

    def __init__(self, app):
        self._app = app

    @property
    def vault(self):
        return self._app.vault

    @property
    def index(self):
        return self._app.index

    def config(self) -> dict:
        return self._app.config()

    def data_dir(self, plugin_id: str) -> Path:
        """プラグインが自由に使える保存場所（<vault>/.mycel/plugins/<id>/）。"""
        d = self._app.vault.internal / "plugins" / plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log(self, plugin_id: str, message: str) -> None:
        sys.stderr.write(f"[plugin:{plugin_id}] {message}\n")


class Plugin:
    id = ""
    name = ""
    description = ""

    def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    def teardown(self) -> None:
        pass

    def on_vault_opened(self) -> None:
        pass

    def before_save(self, event: NoteEvent) -> None:
        pass

    def on_created(self, event: NoteEvent) -> None:
        pass

    def on_saved(self, event: NoteEvent) -> None:
        pass

    def on_deleted(self, event: NoteEvent) -> None:
        pass

    def on_renamed(self, event: NoteEvent) -> None:
        pass

    def routes(self) -> dict[str, Callable[[dict], dict]]:
        """{"名前": handler(params) -> dict}。GET はクエリ、POST は JSON 本文が params。"""
        return {}

    def status(self) -> dict:
        return {}


class PluginManager:
    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self.loaded: dict[str, Plugin] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def available(self) -> list[dict]:
        out = []
        for p in sorted(self.plugin_dir.glob("*.py")):
            if p.stem.startswith("_"):
                continue
            meta = {"id": p.stem, "name": p.stem, "description": "", "enabled": p.stem in self.loaded,
                    "error": self.errors.get(p.stem, "")}
            cls = self._load_class(p.stem, quiet=True)
            if cls:
                meta.update(name=cls.name or p.stem, description=cls.description)
            if p.stem in self.loaded:
                try:
                    meta["status"] = self.loaded[p.stem].status()
                except Exception as e:  # noqa: BLE001
                    meta["status"] = {"error": str(e)}
            out.append(meta)
        return out

    def _load_class(self, pid: str, quiet: bool = False):
        path = self.plugin_dir / f"{pid}.py"
        if not pid.isidentifier() or not path.is_file():
            if not quiet:
                self.errors[pid] = "プラグインが見つかりません"
            return None
        try:
            spec = importlib.util.spec_from_file_location(f"mycel_plugin_{pid}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                self.errors[pid] = f"読み込みエラー: {e}"
            return None
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, Plugin) and obj is not Plugin:
                return obj
        if not quiet:
            self.errors[pid] = "Plugin のサブクラスがありません"
        return None

    def load(self, ids: list[str], ctx: PluginContext) -> None:
        with self._lock:
            self.unload()
            self.errors = {}
            for pid in ids:
                cls = self._load_class(pid)
                if not cls:
                    continue
                try:
                    inst = cls()
                    inst.id = inst.id or pid
                    inst.setup(ctx)
                    self.loaded[pid] = inst
                except Exception as e:  # noqa: BLE001
                    self.errors[pid] = f"初期化エラー: {e}"

    def unload(self) -> None:
        for p in self.loaded.values():
            try:
                p.teardown()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.loaded = {}

    def before_save(self, event: NoteEvent) -> None:
        """PluginVeto だけは呼び出し元へ伝える（保存を止める）。"""
        for p in list(self.loaded.values()):
            try:
                p.before_save(event)
            except PluginVeto:
                raise
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def emit(self, hook: str, *args) -> None:
        """プラグインの例外は本体に波及させない。"""
        for p in list(self.loaded.values()):
            try:
                getattr(p, hook)(*args)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def route(self, pid: str, name: str) -> Callable[[dict], dict] | None:
        p = self.loaded.get(pid)
        if not p:
            return None
        try:
            return p.routes().get(name)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return None
