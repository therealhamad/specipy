"""In-memory run registry and the fan-out behind the SSE relay.

One run == one detected drift == one agent session. Every run keeps its full
event history, so a browser that connects late (or reconnects) is replayed the
whole story before it starts tailing live events.

Events are produced from a worker thread and consumed by the asyncio event
loop, so `emit` hands them across the boundary with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# The five beats the simple-mode UI renders as a timeline.
STAGES = ("detected", "mapping", "fixing", "verifying", "reviewing")

# Terminal states. `verified` is the happy path; `needs_human` is the defined
# verifier-fail path; `error` means the loop itself broke.
TERMINAL = {"verified", "needs_human", "error"}


@dataclass
class Run:
    id: str
    branch: str
    repo: str = ""
    commit: str = ""
    provider_version: str = ""
    drift: dict[str, Any] = field(default_factory=dict)

    status: str = "detected"
    stage: str = "detected"
    simulated: bool = False

    session_id: str | None = None
    console_url: str | None = None

    # Set by CI so the orchestrator knows to wait for a pull request before it
    # posts the Slack resolution message.
    expect_pr: bool = False
    slack_thread_ts: str | None = None

    attempts: int = 0
    pushed: bool = False
    root_cause: str | None = None
    fix: str | None = None
    spoken_summary: str | None = None
    files_changed: list[str] = field(default_factory=list)
    code_diff: str | None = None
    test_output_tail: str | None = None
    audio_url: str | None = None
    audio_source: str | None = None
    error: str | None = None

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    events: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


class RunStore:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ---------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def create(self, run: Run) -> Run:
        with self._lock:
            self._runs[run.id] = run
            self._subscribers.setdefault(run.id, set())
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def latest(self) -> Run | None:
        runs = sorted(self._runs.values(), key=lambda r: r.created_at)
        return runs[-1] if runs else None

    def all(self) -> list[Run]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    # -- events ------------------------------------------------------------

    def emit(self, run_id: str, event_type: str, **data: Any) -> dict[str, Any]:
        """Append an event and push it to every live subscriber.

        Safe to call from a worker thread.
        """
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)

        with self._lock:
            run._seq += 1
            event = {
                "seq": run._seq,
                "ts": time.time(),
                "type": event_type,
                "run_id": run_id,
                "status": run.status,
                "stage": run.stage,
                "simulated": run.simulated,
                **data,
            }
            run.events.append(event)
            run.updated_at = event["ts"]
            queues = list(self._subscribers.get(run_id, ()))

        for queue in queues:
            self._deliver(queue, event)
        return event

    def _deliver(self, queue: asyncio.Queue, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            queue.put_nowait(event)
        else:
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def set_stage(self, run_id: str, stage: str, message: str | None = None) -> None:
        run = self._runs[run_id]
        if run.stage == stage:
            return
        run.stage = stage
        self.emit(run_id, "stage", stage=stage, message=message or "")

    def set_status(self, run_id: str, status: str, message: str | None = None) -> None:
        run = self._runs[run_id]
        run.status = status
        self.emit(run_id, "status", status=status, message=message or "")

    # -- subscriptions -----------------------------------------------------

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.get(run_id, set()).discard(queue)

    def history(self, run_id: str) -> list[dict[str, Any]]:
        run = self._runs.get(run_id)
        return list(run.events) if run else []

    # -- serialisation -----------------------------------------------------

    def snapshot(self, run: Run) -> dict[str, Any]:
        return {
            "id": run.id,
            "branch": run.branch,
            "repo": run.repo,
            "commit": run.commit,
            "provider_version": run.provider_version,
            "status": run.status,
            "stage": run.stage,
            "simulated": run.simulated,
            "is_terminal": run.is_terminal,
            "session_id": run.session_id,
            "console_url": run.console_url,
            "slack_thread_ts": run.slack_thread_ts,
            "expect_pr": run.expect_pr,
            "attempts": run.attempts,
            "pushed": run.pushed,
            "root_cause": run.root_cause,
            "fix": run.fix,
            "spoken_summary": run.spoken_summary,
            "files_changed": run.files_changed,
            "code_diff": run.code_diff,
            "test_output_tail": run.test_output_tail,
            "audio_url": run.audio_url,
            "audio_source": run.audio_source,
            "error": run.error,
            "drift": run.drift,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "event_count": len(run.events),
        }


store = RunStore()
