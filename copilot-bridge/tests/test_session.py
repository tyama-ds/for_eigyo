import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import Config  # noqa: E402
from bridge.fake import FakeWorker  # noqa: E402
from bridge.pool import BridgeError, WorkerPool  # noqa: E402
from bridge.session import SessionStore  # noqa: E402

SYS = {"role": "system", "content": "sys"}
U1 = {"role": "user", "content": "hello"}
A1 = {"role": "assistant", "content": "hi"}
U2 = {"role": "user", "content": "more"}


class StoreTests(unittest.TestCase):
    def test_new_then_continue(self):
        store = SessionStore()
        s, norm, delta = store.resolve([SYS, U1])
        self.assertEqual(delta, [])
        s.sent = norm + [{"role": "assistant", "text": "hi"}]
        s2, norm2, delta2 = store.resolve([SYS, U1, A1, U2])
        self.assertIs(s2, s)
        self.assertEqual([m["text"] for m in delta2], ["more"])

    def test_fork_makes_new_session(self):
        store = SessionStore()
        s, norm, _ = store.resolve([SYS, U1])
        s.sent = norm + [{"role": "assistant", "text": "hi"}]
        s2, _, delta = store.resolve([SYS, U1, {"role": "assistant", "content": "other"}, U2])
        self.assertIsNot(s2, s)
        self.assertEqual(delta, [])
        self.assertEqual(len(store.all()), 2)

    def test_identical_resend_is_fresh(self):
        store = SessionStore()
        s, norm, _ = store.resolve([SYS, U1])
        s.sent = norm + [{"role": "assistant", "text": "hi"}]
        s2, _, delta = store.resolve([SYS, U1])
        self.assertIsNot(s2, s)
        self.assertEqual(delta, [])

    def test_prune(self):
        store = SessionStore(max_sessions=2)
        for i in range(4):
            store.resolve([{"role": "user", "content": str(i)}])
        self.assertEqual(len(store.all()), 2)


class PoolTests(unittest.TestCase):
    def _pool(self, n):
        cfg = Config(backend="fake", windows=n)
        return WorkerPool([FakeWorker(i, cfg) for i in range(n)])

    def test_sticky(self):
        pool = self._pool(2)
        w = pool.acquire("s1", 1)
        pool.release(w)
        w2 = pool.acquire("s2", 1)
        pool.release(w2)
        self.assertIsNot(w, w2)
        self.assertIs(pool.acquire("s1", 1), w)
        self.assertTrue(w.resumed)

    def test_evict_lru(self):
        pool = self._pool(1)
        w = pool.acquire("s1", 1)
        pool.release(w)
        w2 = pool.acquire("s2", 1)
        self.assertIs(w2, w)
        self.assertEqual(w.session_id, "s2")
        self.assertFalse(w.resumed)
        pool.release(w2)
        self.assertFalse(pool.acquire("s1", 1).resumed)  # 取り上げられた会話は続きを送れない

    def test_timeout_when_busy(self):
        pool = self._pool(1)
        pool.acquire("s1", 1)
        with self.assertRaises(BridgeError):
            pool.acquire("s2", 0.2)


if __name__ == "__main__":
    unittest.main()
