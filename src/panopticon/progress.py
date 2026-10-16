"""Command-line progress reporting for long-running, possibly concurrent stages."""

import sys
import threading
import time
from types import TracebackType


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Progress:
    """Tracks a fixed number of tasks, printing a line when each starts and finishes and a
    heartbeat every `interval` seconds listing what is still in flight. Thread-safe; use as a
    context manager so the heartbeat stops when the stage ends.

        with Progress("extract", total=len(chunks)) as progress:
            progress.start(key, "chunk 3 (2026-08-01..2026-08-31)")
            progress.update(key, output_chars=1234)
            progress.finish(key, "41 atoms")
    """

    def __init__(self, stage: str, total: int, interval: float = 15.0) -> None:
        self.stage = stage
        self.total = total
        self.interval = interval
        self.done = 0
        self._began = time.monotonic()
        self._lock = threading.Lock()
        # key -> (label, start time, output chars so far)
        self._active: dict[object, tuple[str, float, int]] = {}
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._beat, daemon=True)

    def __enter__(self) -> "Progress":
        self._print(f"starting {self.total} task(s)")
        self._heartbeat.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._heartbeat.join()
        status = "failed" if exc_type else "finished"
        self._print(f"{status}: {self.done}/{self.total} done")

    def start(self, key: object, label: str) -> None:
        with self._lock:
            self._active[key] = (label, time.monotonic(), 0)
        self._print(f"started {label}")

    def update(self, key: object, output_chars: int) -> None:
        with self._lock:
            if key in self._active:
                label, began, _ = self._active[key]
                self._active[key] = (label, began, output_chars)

    def finish(self, key: object, summary: str) -> None:
        with self._lock:
            label, began, _ = self._active.pop(key)
            self.done += 1
            done = self.done
        took = _clock(time.monotonic() - began)
        self._print(f"[{done}/{self.total}] finished {label} in {took}: {summary}")

    def _print(self, message: str) -> None:
        elapsed = _clock(time.monotonic() - self._began)
        print(f"[{self.stage} {elapsed}] {message}", file=sys.stderr, flush=True)

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            with self._lock:
                active = list(self._active.values())
                done = self.done
            if not active:
                continue
            parts = [
                f"{label} {_clock(now - began)} "
                + (f"{chars / 1000:.1f}k chars out" if chars else "thinking")
                for label, began, chars in active
            ]
            self._print(f"{done}/{self.total} done; in flight: " + "; ".join(parts))
