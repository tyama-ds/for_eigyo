"""API の要求が「どの会話の続きか」を見分ける。

OpenAI 形式の要求は毎回「会話の全部」を持って届き、会話 ID は無い。
そこで (system + 最初の user 発言) の指紋で候補を絞り、前回送った message 列が
今回の先頭と一致する会話を「続き」とみなして、増えた分だけを Copilot へ送る。
一致する会話が無ければ新しい会話を作る（同じ指紋の会話が並行して複数あってもよい。
サブエージェントが同じ指示で複数立つ場合に対応するため）。
"""

from __future__ import annotations

import hashlib
import itertools
import threading
import time

from .protocol import normalize_message, same_message

_counter = itertools.count(1)


def fingerprint(messages: list[dict]) -> str:
    system = "\n".join(m.get("text", "") for m in messages if m["role"] in ("system", "developer"))
    first_user = next((m.get("text", "") for m in messages if m["role"] == "user"), "")
    h = hashlib.sha1((system + "\x00" + first_user).encode("utf-8")).hexdigest()
    return h[:12]


class Session:
    def __init__(self, fp: str):
        self.id = f"s{next(_counter)}"
        self.fingerprint = fp
        self.sent: list[dict] = []   # Copilot の今の会話に送った（正規化済み）message 列
        self.worker_id: str | None = None   # 最後に使った担当（続きを送れるかは pool が判定）
        self.created = time.time()
        self.last_used = self.created
        self.turns = 0
        self.lock = threading.Lock()   # 同じ会話への同時要求を直列化

    def to_dict(self) -> dict:
        return {"id": self.id, "fingerprint": self.fingerprint, "sent": len(self.sent),
                "worker": self.worker_id, "turns": self.turns,
                "last_used": round(self.last_used, 1)}


class SessionStore:
    def __init__(self, max_sessions: int = 200):
        self._by_fp: dict[str, list[Session]] = {}
        self._lock = threading.Lock()
        self.max_sessions = max_sessions

    def resolve(self, raw_messages: list[dict]) -> tuple[Session, list[dict], list[dict]]:
        """(会話, 正規化した全 message, 続きとして送る差分) を返す。差分が空＝作り直しが必要。"""
        norm = [normalize_message(m) for m in raw_messages]
        fp = fingerprint(norm)
        with self._lock:
            best: Session | None = None
            for s in self._by_fp.get(fp, []):
                if s.sent and len(s.sent) < len(norm) and all(
                        same_message(a, b) for a, b in zip(s.sent, norm)):
                    if best is None or len(s.sent) > len(best.sent):
                        best = s
            if best is not None:
                best.last_used = time.time()
                return best, norm, norm[len(best.sent):]
            s = Session(fp)
            self._by_fp.setdefault(fp, []).append(s)
            self._prune()
            return s, norm, []

    def _prune(self) -> None:
        total = sum(len(v) for v in self._by_fp.values())
        if total <= self.max_sessions:
            return
        allsess = sorted((s for v in self._by_fp.values() for s in v), key=lambda s: s.last_used)
        for s in allsess[: total - self.max_sessions]:
            self._by_fp[s.fingerprint].remove(s)
            if not self._by_fp[s.fingerprint]:
                del self._by_fp[s.fingerprint]

    def all(self) -> list[Session]:
        with self._lock:
            return [s for v in self._by_fp.values() for s in v]
