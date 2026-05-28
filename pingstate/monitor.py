"""A Monitor bundles a probe, a state machine, and a background poller thread.

The split is deliberate: PingFSM is the pure state, Probe is the I/O, Monitor
is the glue. Use Monitor when you want background polling for free. Wire
Probe + PingFSM yourself if you want to drive the cadence.
"""

from __future__ import annotations

import threading
from typing import Optional

from .fsm import PingFSM, TRANSITIONS
from .probes import Probe, ProbeResult


class Monitor:
    def __init__(
        self,
        probe: Probe,
        interval: float = 5.0,
        initial_state: str = "unknown",
        transitions: dict | None = None,
        history: int = 20,
    ):
        self.probe = probe
        self.interval = interval
        self.fsm = PingFSM(
            initial=initial_state,
            transitions=transitions if transitions is not None else TRANSITIONS,
            history=history,
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Monitor":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def check_now(self) -> ProbeResult:
        result = self.probe.check()
        event = "ok" if result.ok else "fail"
        self.fsm.fire(event, latency_ms=result.latency_ms, detail=result.detail)
        return result

    def snapshot(self) -> dict:
        snap = self.fsm.snapshot()
        snap["probe"] = self.probe.name
        return snap

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception as e:
                # A probe blowing up shouldn't kill the poller — treat as fail.
                self.fsm.fire("fail", latency_ms=None, detail=f"probe error: {e}")
            self._stop.wait(self.interval)

    def __enter__(self) -> "Monitor":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
