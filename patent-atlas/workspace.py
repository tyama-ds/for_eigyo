"""Ownership and persistence of the existing single-user workspace dictionaries.

Route registration and domain projections remain in their current modules until
Phase 3c. Keeping dictionary identity preserves their existing references.
"""

import copy
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException


@dataclass
class Snapshot:
    """A detached, single-use rollback image; absent selected keys stay absent."""

    state: dict
    keys: tuple | None
    settings: dict | None = None
    job: dict | None = None


class Workspace:
    def __init__(self, data_dir, *, state, settings, job, secret_settings=()):
        self.state = state
        self.settings = settings
        self.job = job
        self.lock = threading.RLock()
        self.data_dir = Path(data_dir)
        self.secret_settings = tuple(secret_settings)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self, *, after_load=None):
        """Merge the existing file format, then run existing recovery policies.

        Unknown fields and the caller-owned dictionary identities are retained.
        Loading/recovering never writes the user's file.
        """
        with self.lock:
            try:
                saved = json.loads((self.data_dir / "workspace.json").read_text(encoding="utf-8"))
                self.state.update(saved.get("state", {}))
                self.settings.update(saved.get("settings", {}))
            except (FileNotFoundError, ValueError):
                pass
            if after_load is not None:
                after_load(self)

    def to_dict(self):
        """Serialization view, consumed under the lock without cloning patents."""
        return {
            "state": self.state,
            "settings": {key: value for key, value in self.settings.items() if key not in self.secret_settings},
        }

    def save(self):
        with self.lock:
            target = self.data_dir / "workspace.json"
            pending = self.data_dir / "workspace.tmp"
            pending.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
            )
            pending.replace(target)

    def idle(self):
        if self.job["status"] == "running":
            raise HTTPException(409, "処理中です。完了を待つか停止してください。")

    def snapshot(self, keys=None, *, include_settings=False, include_job=False):
        with self.lock:
            keys = None if keys is None else tuple(keys)
            return Snapshot(
                state=copy.deepcopy(
                    self.state if keys is None else {k: self.state[k] for k in keys if k in self.state}
                ),
                keys=keys,
                settings=copy.deepcopy(self.settings) if include_settings else None,
                job=copy.deepcopy(self.job) if include_job else None,
            )

    def restore(self, snapshot):
        """Consume a rollback image without replacing shared root dictionaries."""
        with self.lock:
            if snapshot.keys is None:
                self.state.clear()
                self.state.update(snapshot.state)
            else:
                for key in snapshot.keys:
                    if key in snapshot.state:
                        self.state[key] = snapshot.state[key]
                    else:
                        self.state.pop(key, None)
            for target, saved in ((self.settings, snapshot.settings), (self.job, snapshot.job)):
                if saved is not None:
                    target.clear()
                    target.update(saved)

    @contextmanager
    def transaction(
        self,
        keys=None,
        *,
        check_idle=True,
        persist=True,
        save=None,
        include_settings=False,
        include_job=False,
        restore_pending=False,
    ):
        """Lock, check idle, snapshot, commit or restore the selected state.

        Background commits opt out of idle checking; an outer request guard can
        use persist=False when its operation already saves before launching a
        job. The optional save callable keeps the existing service/test seam.
        Only callers that previously restored workspace.tmp request that policy.
        """
        with self.lock:
            if check_idle:
                self.idle()
            before = self.snapshot(keys, include_settings=include_settings, include_job=include_job)
            pending = self.data_dir / "workspace.tmp"
            pending_before = pending.read_bytes() if restore_pending and pending.exists() else None
            try:
                yield self
                if persist:
                    (self.save if save is None else save)()
            except Exception:
                self.restore(before)
                if restore_pending:
                    if pending_before is None:
                        pending.unlink(missing_ok=True)
                    else:
                        pending.write_bytes(pending_before)
                raise

    def public_state(self, project=None):
        """Return a detached projection, including live progress and secret flags.

        Legacy routes still mutate plain dictionaries directly. Do not cache
        projections until those writes have an explicit invalidation boundary.
        """
        with self.lock:
            value = self.state if project is None else project(self.state)
            return copy.deepcopy(
                {
                    **value,
                    "settings": {
                        **{k: v for k, v in self.settings.items() if k not in self.secret_settings},
                        **{key + "_set": bool(self.settings[key]) for key in self.secret_settings},
                    },
                    "job": self.job,
                }
            )
