"""Slack notifications, posted by the orchestrator.

All Slack posting lives here so it behaves identically whether a run was
triggered by CI or by a direct POST.

Why the Web API and not the Slack MCP server: MCP servers are an *agent*
capability, reachable only from inside a CMA session. The first message has to
go out before the agent starts, so the agent could not send it even in
principle. A bot token is also the only way to get a `ts` back, which threading
requires.

Two messages:

  1. `post_detected`  — fired the moment a breaking change is confirmed.
     Returns the message `ts`, which is the thread root.
  2. `post_resolved`  — threaded reply once the verifier has run. Carries the
     spoken briefing as an uploaded audio clip, the PR link, and the timing.
     Renders a different message when verification failed.

Required bot scopes: `chat:write`, `files:write`.

Nothing in here raises into the run. A Slack outage must not fail a fix.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import httpx

from orchestrator import config

API = "https://slack.com/api"


@dataclass
class SlackResult:
    ok: bool
    ts: str | None = None
    detail: str = ""


def enabled() -> bool:
    return bool(config.SLACK_BOT_TOKEN and config.SLACK_CHANNEL_ID)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _call(method: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    response = httpx.post(f"{API}/{method}", headers=_headers(), json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"{method} failed: {body.get('error')} {body.get('response_metadata') or ''}")
    return body


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _vendor(drift: dict[str, Any]) -> str:
    return (drift.get("provider") or {}).get("title") or "an upstream vendor"


def _leaf(location: str) -> str:
    return (location or "").removeprefix("response.").strip()


def _endpoints(drift: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for change in drift.get("breaking") or ():
        operation = change.get("operation")
        if operation and operation not in seen:
            seen.append(operation)
    return seen


def _describe_change(drift: dict[str, Any]) -> list[str]:
    """The removed field, and separately the *guessed* replacement.

    Display-only heuristic: the removed field had an enum, so an added field
    that also has an enum is the most likely replacement. The agent does the
    real mapping, so these are returned as two clearly separate lines — a
    viewer must never mistake this inference for the confirmed result that
    message 2 reports.
    """
    removed = []
    for change in drift.get("breaking") or ():
        name = _leaf(change.get("location", ""))
        if name and name not in removed:
            removed.append(name)

    candidates_with_enum, other_added = [], []
    for change in drift.get("additive") or ():
        name = _leaf(change.get("location", ""))
        if not name or name in other_added or name in candidates_with_enum:
            continue
        if (change.get("now") or {}).get("enum"):
            candidates_with_enum.append(name)
        else:
            other_added.append(name)

    if not removed:
        return ["*Change:* see the diff"]

    lines = [f"*Removed:* {', '.join(f'`{name}`' for name in removed)}"]
    if candidates_with_enum:
        guess = ", ".join(f"`{name}`" for name in candidates_with_enum)
        lines.append(
            f"*Possible replacement:* {guess}   :grey_question: _guess only — "
            "inferred from the spec's shape, not verified. The agent determines "
            "the real mapping._"
        )
    elif other_added:
        new = ", ".join(f"`{name}`" for name in other_added[:3])
        lines.append(
            f"*New fields in the response:* {new}   :grey_question: _any "
            "replacement here is unverified._"
        )
    else:
        lines.append("*Possible replacement:* none in the spec")
    return lines


def _detected_text(run: Any) -> str:
    drift = run.drift or {}
    endpoints = _endpoints(drift)
    count = len(endpoints)
    lines = [
        f":mag: *API drift detected — {_vendor(drift)}*",
        "",
        "A breaking change just shipped from a vendor your codebase depends on.",
        "",
        f"*Endpoint:* {', '.join(f'`{e}`' for e in endpoints) or 'unknown'}",
        *_describe_change(drift),
        f"*Severity:* Breaking — {count} endpoint{'s' if count != 1 else ''} affected",
        "",
        "Investigating impact and generating a fix now…",
    ]
    if run.simulated:
        lines.insert(1, "_(simulated run — no agent session, no code changed)_")
    return "\n".join(lines)


def _elapsed(run: Any) -> str:
    seconds = max(1, int((run.updated_at or time.time()) - run.created_at))
    return f"{seconds}s"


def _resolved_text(run: Any, pr_url: str | None, issue_url: str | None) -> str:
    drift = run.drift or {}
    files = run.files_changed or []
    file_line = (
        ", ".join(f"`{f}`" for f in files) + f" — {len(files)} file{'s' if len(files) != 1 else ''} changed"
        if files
        else "no files changed"
    )

    if run.status == "verified":
        lines = [
            f":white_check_mark: *Fixed & verified — {_vendor(drift)} drift*",
            "",
            run.spoken_summary or run.fix or "A verified fix is ready for review.",
        ]
        # State the confirmed mapping explicitly, so the thread ends on a
        # verified fact rather than leaving the earlier guess as the last word.
        if run.root_cause:
            lines += [
                "",
                f":white_check_mark: *Confirmed:* {run.root_cause}",
                "_Verified by `consumer/test_contract.py`, which the agent cannot modify._",
            ]
        lines += ["", f":page_facing_up: {file_line}"]
        if pr_url:
            lines.append(f":link: Pull request: {pr_url}")
        else:
            lines.append(
                f":link: Branch pushed: `{run.branch}` _(pull request not detected yet)_"
            )
        lines.append(
            f":stopwatch: Detected → fixed → verified in ~{_elapsed(run)}"
            + (f", {run.attempts} test run{'s' if run.attempts != 1 else ''}" if run.attempts else "")
        )
    else:
        lines = [
            f":warning: *Couldn't auto-verify a fix — {_vendor(drift)} drift*",
            "",
            "The change was detected and a fix was attempted, but the contract test "
            "did not pass. Nothing has been claimed as working, and no verified "
            "branch was produced.",
            "",
            f"*Reason:* {run.error or run.fix or 'unknown'}",
        ]
        if run.attempts:
            lines.append(f"*Attempts:* {run.attempts}")
        if issue_url:
            lines.append(f":link: Flagged for human review: {issue_url}")
        elif pr_url:
            lines.append(f":link: Unverified pull request: {pr_url}")
        else:
            lines.append(":link: Flagged for human review — see the CI run")
        lines.append(f":stopwatch: Gave up after ~{_elapsed(run)}")

    if run.simulated:
        lines.insert(1, "_(simulated run — no agent session, no code changed)_")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------


def _playable_copy(path: str) -> tuple[str, bool]:
    """Return a path Slack will render with an inline player.

    Slack shows a player for m4a/AAC most reliably, and the briefing arrives
    from Maya as raw PCM wrapped in WAV. afconvert ships with macOS, so convert
    when it is available and fall back to the WAV otherwise.
    """
    if not path.endswith(".wav") or not shutil.which("afconvert"):
        return path, False
    target = path[: -len(".wav")] + ".m4a"
    try:
        subprocess.run(
            ["afconvert", "-f", "m4af", "-d", "aac", path, target],
            check=True, capture_output=True, timeout=120,
        )
        if os.path.exists(target) and os.path.getsize(target) > 0:
            return target, True
    except Exception:
        pass
    return path, False


def _upload_audio(run: Any, thread_ts: str) -> str:
    """Upload the briefing into the thread. Returns a status string for logging."""
    if not run.audio_url:
        return "no audio to upload"
    local = os.path.join(config.AUDIO_DIR, os.path.basename(run.audio_url))
    if not os.path.exists(local):
        return f"audio file missing at {local}"

    path, converted = _playable_copy(local)
    size = os.path.getsize(path)
    filename = os.path.basename(path)

    try:
        # files.upload is retired; the current flow is get-URL then complete.
        reserve = httpx.post(
            f"{API}/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
            data={"filename": filename, "length": str(size)},
            timeout=30,
        ).json()
        if not reserve.get("ok"):
            return f"getUploadURLExternal failed: {reserve.get('error')}"

        with open(path, "rb") as fh:
            put = httpx.post(
                reserve["upload_url"],
                files={"file": (filename, fh, "audio/mp4" if converted else "audio/wav")},
                timeout=120,
            )
        put.raise_for_status()

        _call(
            "files.completeUploadExternal",
            {
                "files": [{"id": reserve["file_id"], "title": "Spoken briefing"}],
                "channel_id": config.SLACK_CHANNEL_ID,
                "thread_ts": thread_ts,
            },
        )
    except Exception as exc:
        return f"upload failed: {type(exc).__name__}: {exc}"
    return f"uploaded {filename} ({size} bytes, {'m4a' if converted else 'wav'})"


# --------------------------------------------------------------------------
# Pull request discovery
# --------------------------------------------------------------------------


def find_pull_request(branch: str, wait_seconds: int = 0) -> str | None:
    """Look up the PR whose head is `branch`, optionally waiting for CI to open it."""
    if not (config.GITHUB_REPO_URL and config.GITHUB_TOKEN and branch):
        return None
    slug = config.GITHUB_REPO_URL.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    owner = slug.split("/")[0]
    deadline = time.time() + max(0, wait_seconds)
    while True:
        try:
            response = httpx.get(
                f"https://api.github.com/repos/{slug}/pulls",
                params={"head": f"{owner}:{branch}", "state": "all"},
                headers={
                    "Authorization": f"Bearer {config.GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=20,
            )
            if response.status_code < 400:
                rows = response.json()
                if isinstance(rows, list) and rows:
                    return rows[0].get("html_url")
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(3)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def post_detected(run: Any) -> SlackResult:
    if not enabled():
        return SlackResult(False, detail="Slack not configured")
    try:
        body = _call(
            "chat.postMessage",
            {
                "channel": config.SLACK_CHANNEL_ID,
                "text": _detected_text(run),
                "unfurl_links": False,
            },
        )
    except Exception as exc:
        return SlackResult(False, detail=f"{type(exc).__name__}: {exc}")
    return SlackResult(True, ts=body.get("ts"), detail="posted detection alert")


def post_resolved(run: Any, thread_ts: str | None, pr_wait_seconds: int = 0) -> SlackResult:
    if not enabled():
        return SlackResult(False, detail="Slack not configured")

    pr_url = find_pull_request(run.branch, wait_seconds=pr_wait_seconds)
    issue_url = None  # CI files the needs-human issue; not discovered here yet.

    try:
        payload: dict[str, Any] = {
            "channel": config.SLACK_CHANNEL_ID,
            "text": _resolved_text(run, pr_url, issue_url),
            "unfurl_links": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        body = _call("chat.postMessage", payload)
    except Exception as exc:
        return SlackResult(False, detail=f"{type(exc).__name__}: {exc}")

    reply_ts = body.get("ts")
    detail = "posted resolution"
    if run.status == "verified":
        detail += "; " + _upload_audio(run, thread_ts or reply_ts)
    if pr_url:
        detail += f"; pr={pr_url}"
    return SlackResult(True, ts=reply_ts, detail=detail)
