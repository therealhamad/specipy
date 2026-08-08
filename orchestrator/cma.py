"""Drives one Claude Managed Agents session and relays it to the browser.

Two relay details matter enough to call out, because both are near-certain bugs
if you get them the other way round:

1. The event stream is opened *before* the kickoff message is sent. The stream
   only carries events that occur after it opens, so send-then-stream loses the
   opening events and delivers the rest as one buffered lump.

2. The read loop does not break on a bare `session.status_idle`. Idle fires
   transiently — between parallel tool calls, and whenever the session is
   waiting on us. It is terminal only when `stop_reason.type` is not
   `requires_action`. `session.status_terminated` is always terminal.

The SDK's session stream is synchronous, so this whole module runs in a worker
thread and pushes events into the asyncio loop via RunStore.emit.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from orchestrator import config, slack, voice
from orchestrator.prompts import build_kickoff
from orchestrator.runs import Run, RunStore

MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"

_JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_FENCE = re.compile(r"```\s*(\{.*?\})\s*```", re.DOTALL)

# Tool names that tell us which beat of map -> fix -> verify we are in.
_READ_TOOLS = {"read", "grep", "glob"}
_WRITE_TOOLS = {"write", "edit"}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def drive_run(store: RunStore, run: Run) -> None:
    """Run one drift remediation to a terminal state. Never raises."""
    # The detection alert goes out before any agent work starts, so the channel
    # learns about the breakage at the same moment the pipeline does.
    _announce_detected(store, run)
    try:
        if run.simulated:
            _run_simulated(store, run)
            return

        blockers = config.live_mode_blockers()
        if blockers:
            run.error = (
                "Live run not configured. Missing: "
                + ", ".join(blockers)
                + ". Set these in .env, or start the orchestrator with DEMO_MODE=1 "
                "for a clearly-labelled simulated run."
            )
            store.set_status(run.id, "error", run.error)
            # Close the Slack thread we opened, rather than leaving the channel
            # with an "investigating now…" that never resolves.
            _announce_resolved(store, run)
            return

        _run_live(store, run)
    except Exception as exc:  # a crashed driver must still resolve the run
        run.error = f"{type(exc).__name__}: {exc}"
        store.set_status(run.id, "error", run.error)
        _announce_resolved(store, run)


# --------------------------------------------------------------------------
# Live session
# --------------------------------------------------------------------------


def _run_live(store: RunStore, run: Run) -> None:
    import anthropic

    client = anthropic.Anthropic()

    store.emit(run.id, "log", message="Provisioning sandbox and mounting repository")
    session = client.beta.sessions.create(
        agent=config.AGENT_ID,
        environment_id=config.ENVIRONMENT_ID,
        title=f"api-drift {run.id} -> {run.branch}",
        resources=[
            {
                "type": "github_repository",
                "url": config.GITHUB_REPO_URL,
                "authorization_token": config.GITHUB_TOKEN,
                "mount_path": config.GITHUB_MOUNT_PATH,
                "checkout": {"type": "branch", "name": config.GITHUB_BASE_BRANCH},
            }
        ],
        metadata={"run_id": run.id, "branch": run.branch},
    )

    run.session_id = session.id
    run.console_url = config.console_url(session.id)
    store.emit(
        run.id,
        "session.created",
        session_id=session.id,
        console_url=run.console_url,
    )

    kickoff = build_kickoff(
        run_id=run.id,
        branch=run.branch,
        drift=run.drift,
        repo_url=config.GITHUB_REPO_URL,
        mount_path=config.GITHUB_MOUNT_PATH,
        base_branch=config.GITHUB_BASE_BRANCH,
        provider_version=run.provider_version,
    )

    store.set_stage(run.id, "mapping", "Agent is mapping the diff onto consumer code")

    transcript: list[str] = []
    deadline = time.time() + config.SESSION_TIMEOUT_SECONDS
    timed_out = False

    # (1) Stream first. Only then send the kickoff.
    with client.beta.sessions.events.stream(session_id=session.id) as stream:
        client.beta.sessions.events.send(
            session_id=session.id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": kickoff}],
                }
            ],
        )

        for event in stream:
            _handle_event(store, run, event, transcript)

            event_type = getattr(event, "type", "")

            if event_type == "session.status_terminated":
                break

            # (2) Idle is only terminal when it is not waiting on us.
            if event_type == "session.status_idle":
                stop_reason = getattr(event, "stop_reason", None)
                stop_type = getattr(stop_reason, "type", None)
                if stop_type == "requires_action":
                    store.emit(
                        run.id,
                        "log",
                        message="Session idle awaiting client action; continuing to read",
                    )
                    continue
                break

            if time.time() > deadline:
                timed_out = True
                store.emit(
                    run.id,
                    "log",
                    message=(
                        f"Session exceeded {config.SESSION_TIMEOUT_SECONDS}s; "
                        "detaching and marking for human review"
                    ),
                )
                break

    report = _extract_report(transcript)
    _finalise(store, run, report, timed_out=timed_out, client=client)


def _handle_event(store: RunStore, run: Run, event: Any, transcript: list[str]) -> None:
    """Translate one CMA event into a UI event and update the inferred stage."""
    event_type = getattr(event, "type", "") or ""

    if event_type == "agent.message":
        text = _text_of(event)
        if text:
            transcript.append(text)
            store.emit(run.id, "agent.message", text=text)
        return

    if event_type == "agent.thinking":
        store.emit(run.id, "agent.thinking")
        return

    if event_type in {"agent.tool_use", "agent.mcp_tool_use"}:
        name = (getattr(event, "name", "") or "").lower()
        summary = _tool_summary(event)
        store.emit(run.id, "agent.tool_use", tool=name, summary=summary)

        if name in _WRITE_TOOLS and run.stage == "mapping":
            store.set_stage(run.id, "fixing", "Agent is editing consumer code")
        elif name == "bash" and "pytest" in summary:
            run.attempts += 1
            store.set_stage(
                run.id,
                "verifying",
                f"Running the contract test (attempt {run.attempts})",
            )
        elif name in _READ_TOOLS and run.stage == "detected":
            store.set_stage(run.id, "mapping", "Agent is reading the consumer code")
        return

    if event_type in {"agent.tool_result", "agent.mcp_tool_result"}:
        text = _text_of(event)
        if text and ("passed" in text or "failed" in text):
            run.test_output_tail = "\n".join(text.strip().splitlines()[-6:])
        store.emit(run.id, "agent.tool_result", ok=not getattr(event, "is_error", False))
        return

    if event_type == "session.error":
        error = getattr(event, "error", None)
        message = getattr(error, "message", None) or "unknown session error"
        # An MCP server that can't initialise is emitted per turn and does not
        # stop the session. This design doesn't use MCP (Actions opens the PR),
        # so surfacing these as errors would paint a healthy run red.
        if "MCP server" in message and "initialize failed" in message:
            store.emit(run.id, "session.warning", message=message)
        else:
            store.emit(run.id, "session.error", message=message)
        return

    if event_type == "agent.thread_context_compacted":
        store.emit(run.id, "log", message="Session context was compacted")
        return

    if event_type.startswith("session.status_"):
        store.emit(run.id, "session.status", session_status=event_type.rsplit("_", 1)[-1])


def _text_of(event: Any) -> str:
    """Concatenate the text blocks of an event's content, if it has any."""
    content = getattr(event, "content", None)
    if isinstance(content, str):
        return content
    parts = []
    for block in content or ():
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def _tool_summary(event: Any) -> str:
    raw = getattr(event, "input", None)
    if raw is None:
        return ""
    try:
        text = json.dumps(raw) if not isinstance(raw, str) else raw
    except (TypeError, ValueError):
        text = str(raw)
    return text[:300]


# --------------------------------------------------------------------------
# Report parsing and finalisation
# --------------------------------------------------------------------------


def _extract_report(transcript: list[str]) -> dict[str, Any] | None:
    """Pull the last fenced JSON report out of the agent's messages."""
    blob = "\n".join(transcript)
    for pattern in (_JSON_FENCE, _BARE_FENCE):
        matches = pattern.findall(blob)
        for candidate in reversed(matches):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "verdict" in parsed:
                return parsed
    return None


def _fetch_diff(client: Any, session_id: str) -> str | None:
    """Download the agent's fix.diff from the session outputs, if present."""
    for attempt in range(3):
        try:
            listing = client.beta.files.list(
                scope_id=session_id,
                betas=[MANAGED_AGENTS_BETA],
            )
        except Exception:
            return None
        for entry in getattr(listing, "data", []) or []:
            name = getattr(entry, "filename", "") or ""
            if name.endswith(".diff") or name.endswith(".patch"):
                try:
                    downloaded = client.beta.files.download(entry.id)
                    if hasattr(downloaded, "read"):
                        raw = downloaded.read()
                    elif hasattr(downloaded, "content"):
                        raw = downloaded.content
                    else:
                        raw = bytes(downloaded)  # type: ignore[arg-type]
                    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                except Exception:
                    return None
        # Output files take a moment to index after the session goes idle.
        if attempt < 2:
            time.sleep(2)
    return None


def _finalise(
    store: RunStore,
    run: Run,
    report: dict[str, Any] | None,
    *,
    timed_out: bool,
    client: Any | None,
) -> None:
    if report is None:
        run.error = (
            "Session ended without a machine-readable report"
            + (" (timed out)" if timed_out else "")
            + "."
        )
        store.set_stage(run.id, "reviewing")
        store.set_status(run.id, "needs_human", run.error)
        _announce(store, run)
        return

    run.root_cause = report.get("root_cause")
    run.fix = report.get("fix")
    run.spoken_summary = report.get("spoken_summary")
    run.test_output_tail = report.get("test_output_tail") or run.test_output_tail
    files = report.get("files_changed")
    if isinstance(files, list):
        run.files_changed = [str(f) for f in files]
    if isinstance(report.get("attempts"), int):
        run.attempts = report["attempts"]

    if client is not None and run.session_id:
        run.code_diff = _fetch_diff(client, run.session_id)

    reported_branch = (report.get("branch") or "").strip()
    verdict = (report.get("verdict") or "").strip().lower()
    pushed = bool(report.get("pushed"))
    run.pushed = pushed

    store.set_stage(run.id, "reviewing")

    # The agent is told to echo the branch it was given. A mismatch means the
    # handoff assumption is broken, so refuse to call it verified.
    if reported_branch and reported_branch != run.branch:
        run.error = (
            f"Agent reported branch {reported_branch!r} but was given {run.branch!r}."
        )
        store.set_status(run.id, "needs_human", run.error)
        _announce(store, run)
        return

    if verdict == "verified" and pushed and not timed_out:
        store.set_status(
            run.id, "verified", "Contract test passed; branch pushed for review"
        )
    else:
        reason = run.fix or "Agent did not reach a verified fix."
        if timed_out:
            reason = "Session timed out before verification. " + reason
        elif verdict == "verified" and not pushed:
            reason = "Agent reported a passing test but did not push a branch. " + reason
        run.error = reason
        store.set_status(run.id, "needs_human", reason)

    _announce(store, run)


def _announce_detected(store: RunStore, run: Run) -> None:
    """Slack message 1: a breaking change was confirmed."""
    if not slack.enabled():
        store.emit(run.id, "slack.skipped", reason="Slack not configured")
        return
    result = slack.post_detected(run)
    run.slack_thread_ts = result.ts
    store.emit(
        run.id,
        "slack.detected" if result.ok else "slack.error",
        ok=result.ok,
        thread_ts=result.ts,
        detail=result.detail,
    )


def _announce(store: RunStore, run: Run) -> None:
    """Produce the spoken briefing, then post the threaded Slack resolution.

    Voice first: the SSE relay waits briefly for one event after the terminal
    status, and Slack can take tens of seconds when it waits for CI's PR.
    """
    if run.status == "verified":
        text = run.spoken_summary or run.fix or "A verified fix is ready for review."
        result = voice.synthesize(text, run.id)
        run.audio_url = result.url
        run.audio_source = result.source
        store.emit(
            run.id,
            "voice.ready",
            audio_url=result.url,
            audio_source=result.source,
            text=text,
            detail=result.detail,
        )
    else:
        store.emit(run.id, "voice.skipped", reason=f"status is {run.status}")

    _announce_resolved(store, run)


def _announce_resolved(store: RunStore, run: Run) -> None:
    """Slack message 2: threaded reply with the outcome, audio and PR link."""
    if not slack.enabled():
        return
    # Only wait for a pull request when CI is the one opening it.
    wait = config.SLACK_PR_WAIT_SECONDS if run.expect_pr else 0
    if wait:
        store.emit(run.id, "log", message=f"Waiting up to {wait}s for CI to open the PR")
    result = slack.post_resolved(run, run.slack_thread_ts, pr_wait_seconds=wait)
    store.emit(
        run.id,
        "slack.resolved" if result.ok else "slack.error",
        ok=result.ok,
        threaded=bool(run.slack_thread_ts),
        detail=result.detail,
    )


# --------------------------------------------------------------------------
# Simulated run (labelled, for the backup demo path)
# --------------------------------------------------------------------------

_SIM_NOTE = (
    "Simulated run. A live run renders the agent's real `git diff` here, "
    "downloaded from the session outputs."
)


def _run_simulated(store: RunStore, run: Run) -> None:
    """Replay the shape of a real run without calling the API.

    Deliberately does not contain the correct fix: the point of the demo is that
    the agent derives it. This path exercises the relay, the timeline, the voice
    hand-off and both interface modes.
    """
    beats: list[tuple[float, str, dict[str, Any]]] = [
        (0.6, "log", {"message": "Provisioning sandbox and mounting repository"}),
        (0.8, "session.created", {"session_id": f"sesn_simulated_{run.id}", "console_url": None}),
        (0.9, "stage:mapping", {"message": "Agent is mapping the diff onto consumer code"}),
        (1.0, "agent.tool_use", {"tool": "grep", "summary": '{"pattern": "transaction_status"}'}),
        (0.8, "agent.tool_use", {"tool": "read", "summary": '{"path": "consumer/normalize.py"}'}),
        (0.8, "agent.tool_use", {"tool": "read", "summary": '{"path": "consumer/test_contract.py"}'}),
        (1.0, "stage:fixing", {"message": "Agent is editing consumer code"}),
        (1.0, "agent.tool_use", {"tool": "edit", "summary": '{"path": "consumer/normalize.py"}'}),
        (1.0, "stage:verifying", {"message": "Running the contract test (attempt 1)"}),
        (1.4, "agent.tool_result", {"ok": True}),
        (0.6, "stage:reviewing", {"message": "Pushing branch for review"}),
    ]

    run.session_id = f"sesn_simulated_{run.id}"
    for delay, kind, payload in beats:
        time.sleep(delay)
        if kind.startswith("stage:"):
            store.set_stage(run.id, kind.split(":", 1)[1], payload.get("message"))
        else:
            store.emit(run.id, kind, **payload)

    run.attempts = 1
    run.root_cause = (
        "The provider removed the transaction status field from its payment "
        "responses and replaced it with a differently-named, nested field that "
        "uses a new set of status values."
    )
    run.fix = "Simulated run: no code was changed."
    run.files_changed = ["consumer/normalize.py"]
    run.code_diff = _SIM_NOTE
    run.test_output_tail = "(simulated)"
    run.spoken_summary = (
        "Our payments provider quietly changed how it reports the state of a "
        "payment, and our finance dashboard stopped recognising completed "
        "payments as revenue. Nothing looked broken, so nobody was alerted. "
        "This is a simulated run of the repair loop, so no code was changed."
    )
    store.set_status(run.id, "verified", "Simulated run complete")
    _announce(store, run)
