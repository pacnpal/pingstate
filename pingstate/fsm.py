"""The state machine.

Events come from probe results, either "ok" or "fail". States go unknown, then
up or down, with degraded sitting in between. A host that's up drops to degraded
on the first failure, not straight to down, so a single blip doesn't flap the
whole status.

Transitions are a plain dict so you can pass a custom table to PingFSM if you
want different behavior (e.g. no degraded tier, or N failures before flipping).
"""

import threading
import time
from collections import deque


TRANSITIONS = {
    ("unknown", "ok"): "up",
    ("unknown", "fail"): "down",
    ("up", "ok"): "up",
    ("up", "fail"): "degraded",
    ("degraded", "ok"): "up",
    ("degraded", "fail"): "down",
    ("down", "ok"): "up",
    ("down", "fail"): "down",
}


class PingFSM:
    def __init__(self, initial="unknown", transitions=None, history=20):
        self.state = initial
        self.transitions = transitions if transitions is not None else TRANSITIONS
        self.lock = threading.Lock()
        self.last_event = None
        self.last_detail = None
        self.last_change = time.time()
        self.last_check = None
        self.last_latency_ms = None
        self.transitions_count = 0
        self.recent = deque(maxlen=history)

    def fire(self, event, latency_ms=None, detail=None):
        with self.lock:
            key = (self.state, event)
            next_state = self.transitions.get(key, self.state)
            now = time.time()
            if next_state != self.state:
                self.state = next_state
                self.last_change = now
                self.transitions_count += 1
            self.last_event = event
            self.last_detail = detail
            self.last_check = now
            self.last_latency_ms = latency_ms
            self.recent.append(
                {"ts": now, "event": event, "state": self.state, "detail": detail}
            )
            return self.state

    def snapshot(self):
        with self.lock:
            now = time.time()
            return {
                "state": self.state,
                "last_event": self.last_event,
                "last_detail": self.last_detail,
                "last_latency_ms": self.last_latency_ms,
                "uptime_in_state_s": round(now - self.last_change, 1),
                "last_check_ts": self.last_check,
                "last_check_age_s": (
                    round(now - self.last_check, 1) if self.last_check else None
                ),
                "transitions": self.transitions_count,
                "recent": list(self.recent),
            }
