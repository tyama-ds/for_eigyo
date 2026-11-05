"""Ownership and persistence of the existing single-user workspace dictionaries.

Keeping dictionary identity preserves existing route and service references.
"""

import copy
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from orchestration_engine import recover
from workspace_defaults import new_workbench


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
        self.stop = threading.Event()
        self.data_dir = Path(data_dir)
        self.secret_settings = tuple(secret_settings)
        self._public_cache = None
        self._public_json = None
        self._public_signature = None
        self._public_project = None
        self._public_token = None
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self, *, after_load=None, initialize_workflows=False):
        """Merge the existing file format, then run existing recovery policies.

        Unknown fields and the caller-owned dictionary identities are retained.
        Loading/recovering never writes the user's file.
        """
        with self.lock:
            self.invalidate_public_state()
            try:
                saved = json.loads((self.data_dir / "workspace.json").read_text(encoding="utf-8"))
                self.state.update(saved.get("state", {}))
                self.settings.update(saved.get("settings", {}))
            except (FileNotFoundError, ValueError):
                pass
            if after_load is not None:
                after_load(self)
            if initialize_workflows:
                self._initialize_workflows()

    def _initialize_workflows(self):
        self.state["orchestration"] = recover(self.state.get("orchestration"))
        if not isinstance(self.state.get("research_workbench"), dict):
            self.state["research_workbench"] = new_workbench()
            self.state["research_workbench"]["brief"]["keywords"] = self.state.get("keywords", "")

    def to_dict(self):
        """Serialization view, consumed under the lock without cloning patents."""
        return {
            "state": self.state,
            "settings": {key: value for key, value in self.settings.items() if key not in self.secret_settings},
        }

    def save(self):
        with self.lock:
            self.invalidate_public_state()
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
            self.invalidate_public_state()
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
                self.invalidate_public_state()
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

    def invalidate_public_state(self):
        with self.lock:
            self._public_cache = None
            self._public_json = None
            self._public_signature = None
            self._public_project = None
            self._public_token = None

    def _cached_public_state(self, project, cache_token):
        """Called under lock. Detect legacy nested writes, even without save().

        The digest costs one JSON traversal but avoids repeated domain projection
        and deep copies on unchanged polling requests. Secrets only enter the
        ephemeral digest input, never the cached public payload. Non-JSON domain
        values retain the old dict-returning behavior without caching.
        """
        token = cache_token() if cache_token is not None else None
        try:
            signature = hashlib.sha256(
                json.dumps((self.state, self.settings, self.job), allow_nan=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).digest()
        except (TypeError, ValueError, OverflowError):
            signature = None
        if (
            signature is not None
            and self._public_cache is not None
            and self._public_signature == signature
            and self._public_project is project
            and self._public_token == token
        ):
            return self._public_cache
        self.invalidate_public_state()
        value = self.state if project is None else project(self.state)
        result = copy.deepcopy(
            {
                **value,
                "settings": {
                    **{k: v for k, v in self.settings.items() if k not in self.secret_settings},
                    **{key + "_set": bool(self.settings[key]) for key in self.secret_settings},
                },
                "job": self.job,
            }
        )
        if signature is not None:
            self._public_cache = result
            self._public_signature = signature
            self._public_project = project
            self._public_token = token
        return result

    def public_state(self, project=None, *, cache_token=None):
        """Return a detached dict; callers cannot modify the cached projection."""
        with self.lock:
            return copy.deepcopy(self._cached_public_state(project, cache_token))

    def public_state_json(self, project=None, *, cache_token=None):
        """Immutable JSON for HTTP polling; no deep copy on a cache hit."""
        with self.lock:
            value = self._cached_public_state(project, cache_token)
            if self._public_json is None:
                self._public_json = json.dumps(
                    jsonable_encoder(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
            return self._public_json
