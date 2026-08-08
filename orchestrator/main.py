"""Orchestrator: takes a drift report from CI, drives the agent, relays to the UI.

GitHub Actions POSTs the diff here (over an ngrok tunnel), which keeps the real
CI trigger without asking an ephemeral Actions job to hold a live connection
open for the browser.

    uvicorn orchestrator.main:app --port 8000
    ngrok http 8000

    POST /drift              start a run (from CI)
    POST /demo/drift         start a run from the committed sample diff (local)
    GET  /runs               list runs
    GET  /runs/{id}          snapshot — what CI polls before opening the PR
    GET  /events/{id}        SSE relay — history replay, then live tail
    GET  /                   the dual-mode interface
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from orchestrator import config
from orchestrator.cma import drive_run
from orchestrator.runs import Run, store

INTERFACE_DIR = os.path.join(config.REPO_ROOT, "interface")
STATIC_DIR = os.path.join(INTERFACE_DIR, "static")
SAMPLE_DRIFT = os.path.join(config.REPO_ROOT, "specs", "sample_drift.json")

HEARTBEAT_SECONDS = 15.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The CMA driver runs in a worker thread and needs this loop to hand events
    # back to the SSE subscribers.
    store.bind_loop(asyncio.get_running_loop())
    os.makedirs(config.AUDIO_DIR, exist_ok=True)
    yield


app = FastAPI(title="API Drift Auto-Adapt Orchestrator", lifespan=lifespan)


class DriftPayload(BaseModel):
    branch: str = Field(..., description="Deterministic branch name from CI.")
    drift: dict[str, Any] = Field(..., description="Envelope from scripts.detect_drift.")
    run_id: str | None = None
    repo: str = ""
    commit: str = ""
    provider_version: str = ""
    simulated: bool | None = None
    expect_pr: bool = False


def _start(payload: DriftPayload) -> Run:
    run_id = (payload.run_id or uuid.uuid4().hex[:10]).strip()
    if store.get(run_id):
        raise HTTPException(status_code=409, detail=f"run {run_id} already exists")

    simulated = config.DEMO_MODE if payload.simulated is None else payload.simulated
    run = store.create(
        Run(
            id=run_id,
            branch=payload.branch,
            repo=payload.repo or config.GITHUB_REPO_URL,
            commit=payload.commit,
            provider_version=payload.provider_version,
            drift=payload.drift,
            simulated=simulated,
            expect_pr=payload.expect_pr,
        )
    )

    breaking = payload.drift.get("breaking") or []
    store.emit(
        run.id,
        "drift.detected",
        summary=payload.drift.get("summary", ""),
        breaking_count=len(breaking),
        tool=payload.drift.get("tool"),
        provider=payload.drift.get("provider"),
        branch=run.branch,
    )

    threading.Thread(
        target=drive_run, args=(store, run), name=f"drift-{run.id}", daemon=True
    ).start()
    return run


@app.post("/drift")
def post_drift(payload: DriftPayload) -> dict[str, Any]:
    run = _start(payload)
    return {
        "run_id": run.id,
        "branch": run.branch,
        "simulated": run.simulated,
        "events_url": f"/events/{run.id}",
        "ui_url": f"/?run={run.id}",
    }


@app.post("/demo/drift")
def post_demo_drift(request: Request) -> dict[str, Any]:
    """Start a run from the committed sample diff, without CI or Docker."""
    if not os.path.exists(SAMPLE_DRIFT):
        raise HTTPException(
            status_code=500,
            detail=(
                "specs/sample_drift.json is missing. Generate it with: "
                "python -m scripts.detect_drift specs/provider.baseline.json <v2 spec> "
                "--out specs/sample_drift.json"
            ),
        )
    with open(SAMPLE_DRIFT) as fh:
        drift = json.load(fh)

    run_id = uuid.uuid4().hex[:10]
    simulated = request.query_params.get("simulated")
    payload = DriftPayload(
        branch=f"api-drift/local-{run_id}",
        drift=drift,
        run_id=run_id,
        repo=config.GITHUB_REPO_URL,
        provider_version="v2",
        simulated=(simulated.lower() in {"1", "true", "yes"}) if simulated else None,
    )
    run = _start(payload)
    return {"run_id": run.id, "branch": run.branch, "simulated": run.simulated}


@app.get("/runs")
def get_runs() -> dict[str, Any]:
    return {
        "runs": [
            {
                "id": r.id,
                "branch": r.branch,
                "status": r.status,
                "stage": r.stage,
                "simulated": r.simulated,
                "created_at": r.created_at,
            }
            for r in store.all()
        ]
    }


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
    return store.snapshot(run)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "demo_mode": config.DEMO_MODE,
        "live_blockers": config.live_mode_blockers(),
        "runs": len(store.all()),
    }


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


@app.get("/events/{run_id}")
async def events(run_id: str, request: Request) -> StreamingResponse:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

    async def generator() -> AsyncIterator[str]:
        # Subscribe before replaying history so nothing emitted in between is
        # lost; duplicates are filtered by sequence number.
        queue = store.subscribe(run_id)
        try:
            highest = 0
            for event in store.history(run_id):
                highest = max(highest, event["seq"])
                yield _sse(event)

            snapshot = store.snapshot(store.get(run_id))  # type: ignore[arg-type]
            yield _sse({"type": "snapshot", "seq": highest, **snapshot})

            if snapshot["is_terminal"]:
                yield _sse({"type": "done", "seq": highest, "status": snapshot["status"]})
                return

            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                if event["seq"] <= highest:
                    continue
                highest = event["seq"]
                yield _sse(event)

                status = event.get("status")
                if event["type"] == "status" and status in {
                    "verified",
                    "needs_human",
                    "error",
                }:
                    final = store.snapshot(store.get(run_id))  # type: ignore[arg-type]
                    yield _sse({"type": "snapshot", "seq": highest, **final})

                    # A verified or needs-human run emits one voice event right
                    # after its terminal status; wait briefly so the UI gets the
                    # audio on this same connection. An errored run emits none.
                    if status in {"verified", "needs_human"}:
                        try:
                            late = await asyncio.wait_for(queue.get(), timeout=10.0)
                            highest = late["seq"]
                            yield _sse(late)
                            yield _sse(
                                {
                                    "type": "snapshot",
                                    "seq": highest,
                                    **store.snapshot(store.get(run_id)),  # type: ignore[arg-type]
                                }
                            )
                        except asyncio.TimeoutError:
                            pass

                    yield _sse({"type": "done", "seq": highest, "status": status})
                    return
        finally:
            store.unsubscribe(run_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(INTERFACE_DIR, "index.html"))


os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
