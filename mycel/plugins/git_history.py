"""Git 履歴: 保存のたびに（少し待ってまとめて）Vault を git commit する。

- Vault が git リポジトリでなければ ``git init`` する。``.mycel/`` は .gitignore に入れる
- 作成者名は設定の ``user_name`` を使う
- ``git`` コマンドが無い環境では何もしない（状態に表示）
- 共有（チーム Vault）に進むときは、このプラグインに push / pull を足すのが近道
"""
from __future__ import annotations

import shutil
import subprocess
import threading

from mycelcore.plugins import Plugin

DEBOUNCE_SECONDS = 20.0


class GitHistory(Plugin):
    name = "Git 履歴"
    description = "保存をまとめて Vault を git commit し、ノートの変更履歴を残します（git が必要）"

    def setup(self, ctx) -> None:
        super().setup(ctx)
        self.git = shutil.which("git")
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self.last_error = ""
        if self.git:
            self._ensure_repo()

    def teardown(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
        self._commit()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.git, *args], cwd=self.ctx.vault.root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)

    def _ensure_repo(self) -> None:
        root = self.ctx.vault.root
        if not (root / ".git").exists():
            r = self._run("init")
            if r.returncode != 0:
                self.last_error = r.stderr.strip()
                return
        ignore = root / ".gitignore"
        lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
        if ".mycel/" not in lines:
            ignore.write_text("\n".join(lines + [".mycel/"]) + "\n", encoding="utf-8")

    def _schedule(self, event) -> None:
        if not self.git:
            return
        with self._lock:
            self._pending.add(event.path)
            if event.old_path:
                self._pending.add(event.old_path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._commit)
            self._timer.daemon = True
            self._timer.start()

    on_created = on_saved = on_deleted = on_renamed = _schedule

    def _commit(self) -> None:
        if not self.git:
            return
        with self._lock:
            paths, self._pending = sorted(self._pending), set()
        if not paths:
            return
        author = self.ctx.config()["user_name"] or "mycel"
        self._run("add", "-A")
        msg = "Mycel: " + (", ".join(paths[:3]) + (f" ほか {len(paths) - 3} 件" if len(paths) > 3 else ""))
        r = self._run("-c", f"user.name={author}", "-c", f"user.email={author}@localhost",
                      "commit", "-q", "-m", msg)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            self.last_error = (r.stderr or r.stdout).strip()[:300]

    def log(self, path: str = "", limit: int = 30) -> list[dict]:
        if not self.git:
            return []
        args = ["log", f"-{limit}", "--pretty=format:%h%x09%an%x09%ad%x09%s", "--date=iso"]
        if path:
            args += ["--", path]
        r = self._run(*args)
        out = []
        for line in r.stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                out.append(dict(zip(("commit", "author", "date", "message"), parts)))
        return out

    def routes(self):
        return {"log": lambda p: {"log": self.log(p.get("path", ""), int(p.get("limit") or 30))},
                "commit_now": self._commit_now}

    def _commit_now(self, params: dict) -> dict:
        if params.get("_method") != "POST":
            return {"error": "POST で呼んでください"}
        self._commit()
        return {"ok": True}

    def status(self) -> dict:
        if not self.git:
            return {"error": "git コマンドが見つかりません"}
        return {"pending": len(self._pending), "error": self.last_error}
