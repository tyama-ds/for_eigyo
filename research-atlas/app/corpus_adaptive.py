"""Report-scoped, text-free local generation sizing and bounded timeout recovery.

Elapsed time per input unit is a conservative scheduling heuristic, not a model
throughput benchmark: loading, output length and server load also affect it.
"""
from __future__ import annotations

from copy import deepcopy
import math

POLICY_VERSION = "corpus-adaptive-v1"
TIMEOUT_KINDS = {"first_response_timeout", "read_timeout", "total_timeout"}
MAX_TIMEOUT_RECOVERIES = 3
TARGET_SECONDS = 120.0


def _integer(value, default=0, upper=1_000_000_000):
    return value if type(value) is int and 0 <= value <= upper else default


def _number(value, default=0.0):
    return float(value) if type(value) in {int, float} and 0 <= value <= 1e12 and math.isfinite(value) else default


class AdaptivePolicy:
    def __init__(self, handle, namespace, provider, *, max_chars=5000, max_tokens=2400, max_items=8):
        self.handle = handle if isinstance(handle, dict) else {}
        previous = self.handle.get("state")
        previous = previous if isinstance(previous, dict) and previous.get("version") == POLICY_VERSION and previous.get("cache_namespace") == namespace else {}
        self.maximum = {"extraction": max_chars, "synthesis": max_tokens}
        self.minimum = {"extraction": min(160, max_chars), "synthesis": min(320, max_tokens)}
        self.max_items = max_items
        self.enabled = provider == "local"
        self.state = {"version": POLICY_VERSION, "cache_namespace": namespace, "enabled": self.enabled,
                      "target_seconds": TARGET_SECONDS, "max_timeout_recoveries": MAX_TIMEOUT_RECOVERIES,
                      "timeout_recoveries": _integer(previous.get("timeout_recoveries")),
                      "total_requests": _integer(previous.get("total_requests")), "events": []}
        for phase, maximum in self.maximum.items():
            old = previous.get(phase, {})
            old = old if isinstance(old, dict) else {}
            key = "max_chars" if phase == "extraction" else "max_tokens"
            self.state[phase] = {key: max(self.minimum[phase], min(maximum, _integer(old.get(key), maximum))),
                "successful_requests": _integer(old.get("successful_requests")),
                "observed_seconds": _number(old.get("observed_seconds")),
                "observed_units": _integer(old.get("observed_units")),
                "ewma_seconds_per_unit": _number(old.get("ewma_seconds_per_unit"))}
            if phase == "synthesis":
                self.state[phase]["max_items"] = max(min(2, max_items), min(max_items, _integer(old.get("max_items"), max_items)))
        for event in previous.get("events", [])[-32:] if isinstance(previous.get("events"), list) else []:
            if (isinstance(event, dict) and isinstance(event.get("phase"), str) and event["phase"] in self.maximum
                    and isinstance(event.get("reason"), str) and event["reason"] in {"measured_speed", "timeout_split", "timeout_retry", "timeout_recovery_exhausted"}):
                self.state["events"].append({key: event[key] for key in ("phase", "reason", "kind", "previous_limit", "new_limit", "elapsed_seconds", "recovery")
                    if key in event and ((key in {"phase", "reason"}) or
                        (key == "kind" and isinstance(event[key], str) and event[key] in TIMEOUT_KINDS) or
                        (key not in {"phase", "reason", "kind"} and type(event[key]) in {int, float} and 0 <= event[key] <= 1e12 and math.isfinite(event[key])))})
        self.recoveries = 0
        self.small_retries = set()

    def persist(self):
        value = deepcopy(self.state)
        save = self.handle.get("save")
        if callable(save):
            save(value)
        self.handle["state"] = value

    def limit(self, phase):
        return self.state[phase]["max_chars" if phase == "extraction" else "max_tokens"] if self.enabled else self.maximum[phase]

    def _event(self, event, progress):
        self.state["events"] = [*self.state["events"], event][-32:]
        self.persist()
        if callable(progress):
            stages = {"measured_speed": "実測した処理時間に合わせて後続の入力を小分けにします。",
                      "timeout_split": "時間切れの入力を分割し、上限付きで再試行します。",
                      "timeout_retry": "最小入力を一度だけ再試行します。",
                      "timeout_recovery_exhausted": "時間切れの再試行上限に達しました。未処理部分を保持して停止します。"}
            progress({"stage": stages[event["reason"]], "adaptive": deepcopy(event)})

    def started(self):
        if self.enabled:
            self.state["total_requests"] += 1

    def success(self, phase, units, elapsed, progress=None):
        if not self.enabled:
            return
        # Mock/clock anomalies and tiny inputs are not a useful speed sample.
        if not (type(elapsed) in {int, float} and math.isfinite(elapsed) and 1 <= elapsed <= 86_400
                and type(units) is int and units >= (160 if phase == "extraction" else 80)):
            self.persist()
            return
        part = self.state[phase]
        old_rate = part["ewma_seconds_per_unit"]
        rate = elapsed / units
        rate = rate if not old_rate else 0.7 * old_rate + 0.3 * rate
        part["ewma_seconds_per_unit"] = round(rate, 8)
        part["successful_requests"] += 1
        part["observed_seconds"] = round(part["observed_seconds"] + elapsed, 3)
        part["observed_units"] += units
        before = self.limit(phase)
        # Never increase a learned cap. Limit one observation to a halving and
        # reserve 20% headroom; a lower bound avoids pathological micro-requests.
        after = max(self.minimum[phase], min(before, max(before // 2, int(TARGET_SECONDS * 0.8 / rate))))
        self._set_limit(phase, after)
        if after < before:
            self._event({"phase": phase, "reason": "measured_speed", "previous_limit": before,
                         "new_limit": after, "elapsed_seconds": round(elapsed, 3)}, progress)
        else:
            self.persist()

    def _set_limit(self, phase, limit):
        self.state[phase]["max_chars" if phase == "extraction" else "max_tokens"] = limit
        if phase == "synthesis":
            self.state[phase]["max_items"] = min(self.state[phase]["max_items"], max(min(2, self.max_items),
                math.ceil(self.max_items * limit / self.maximum[phase])))

    def recovery(self, phase, kind, *, divisible, request_key, units, progress=None):
        """Return split/retry/stop, sharing one bounded budget per invocation."""
        if not self.enabled or kind not in TIMEOUT_KINDS:
            return None
        before = self.limit(phase)
        if self.recoveries >= MAX_TIMEOUT_RECOVERIES or (not divisible and request_key in self.small_retries):
            self._event({"phase": phase, "reason": "timeout_recovery_exhausted", "kind": kind,
                         "previous_limit": before, "new_limit": before, "recovery": self.recoveries}, progress)
            return "stop"
        self.recoveries += 1
        self.state["timeout_recoveries"] += 1
        after = max(self.minimum[phase], min(before, max(1, units // 2)))
        self._set_limit(phase, after)
        if not divisible:
            self.small_retries.add(request_key)
        self._event({"phase": phase, "reason": "timeout_split" if divisible else "timeout_retry", "kind": kind,
                     "previous_limit": before, "new_limit": after, "recovery": self.recoveries}, progress)
        return "split" if divisible else "retry"
