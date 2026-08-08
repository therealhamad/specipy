"""Ask the real agent what its sandbox can actually do, before trusting a run.

No repository is mounted, so this works before the demo repo exists. It answers
the questions that decide whether a real run can succeed:

  * is there a Python, and is pytest importable without network access?
  * can the sandbox reach PyPI (i.e. did `allow_package_managers` matter)?
  * does the git binary exist for the eventual push?

It also exercises the live relay: stream-opened-before-kickoff, and the
idle-vs-terminated termination gate, against the real API rather than a script.

    python -m scripts.probe_sandbox
"""

from __future__ import annotations

import sys
import time

import anthropic

from orchestrator import config

PROMPT = """\
You are being used as an environment probe. Do not write or modify any files.

Run these commands with bash and report the raw output of each:

1. python3 -V
2. python3 -m pytest --version   (if it fails, say so — do not try to install it)
3. python3 -c "import pytest, sys; print('pytest importable', pytest.__version__)"
4. git --version
5. pip download --no-deps -d /tmp/probe pytest 2>&1 | tail -3   (expected to fail if egress is blocked)

Then end with a plain summary of exactly four lines:
PYTHON: <version or missing>
PYTEST: <version or missing>
GIT: <version or missing>
PYPI_REACHABLE: <yes or no>
"""


def main() -> int:
    blockers = [b for b in config.live_mode_blockers() if b in {"ANTHROPIC_API_KEY", "AGENT_ID", "ENVIRONMENT_ID"}]
    if blockers:
        print(f"missing: {', '.join(blockers)}", file=sys.stderr)
        return 2

    client = anthropic.Anthropic()
    session = client.beta.sessions.create(
        agent=config.AGENT_ID,
        environment_id=config.ENVIRONMENT_ID,
        title="sandbox capability probe",
    )
    print(f"session: {session.id}")
    print(f"trace:   {config.console_url(session.id)}\n")

    started = time.time()
    transcript: list[str] = []

    # Stream first, then send — the same ordering the orchestrator uses.
    with client.beta.sessions.events.stream(session_id=session.id) as stream:
        client.beta.sessions.events.send(
            session_id=session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": PROMPT}]}],
        )
        for event in stream:
            kind = getattr(event, "type", "")
            elapsed = f"{time.time() - started:6.1f}s"

            if kind == "agent.message":
                text = "".join(
                    getattr(b, "text", "") or ""
                    for b in (getattr(event, "content", None) or ())
                    if getattr(b, "type", None) == "text"
                )
                if text.strip():
                    transcript.append(text)
                    print(f"[{elapsed}] agent: {text.strip()[:600]}")
            elif kind == "agent.tool_use":
                print(f"[{elapsed}] tool:  {getattr(event, 'name', '?')} {str(getattr(event, 'input', ''))[:160]}")
            elif kind == "session.error":
                err = getattr(event, "error", None)
                print(f"[{elapsed}] ERROR: {getattr(err, 'message', err)}")
            elif kind.startswith("session.status_"):
                print(f"[{elapsed}] {kind}")

            if kind == "session.status_terminated":
                break
            if kind == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None)
                print(f"[{elapsed}] idle, stop_reason={stop_type}")
                if stop_type == "requires_action":
                    continue
                break
            if time.time() - started > 600:
                print("probe timed out")
                break

    print("\n=== verdict lines ===")
    blob = "\n".join(transcript)
    for line in blob.splitlines():
        if line.strip().startswith(("PYTHON:", "PYTEST:", "GIT:", "PYPI_REACHABLE:")):
            print(" ", line.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
