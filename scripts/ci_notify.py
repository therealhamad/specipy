"""Hand a drift envelope to the orchestrator, wait for the verdict, prep the PR.

Called from GitHub Actions. Keeps the branch-name handoff deterministic: the
branch string is generated here from GITHUB_RUN_ID, sent to the orchestrator,
and reused verbatim by the PR step — it is never parsed back out of the agent's
event stream.

    python -m scripts.ci_notify --drift /tmp/drift.json --branch api-drift/123

Writes GitHub step outputs (run_id, status, pushed, verified, branch) and the PR
body to --body-out. Exit code is 0 whenever the pipeline itself worked, even
when the agent could not verify a fix — an unverified fix is a defined outcome,
not a CI failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

POLL_SECONDS = 5

# Where the orchestrator's public URL comes from, in precedence order:
#   1. --base-url / the ORCHESTRATOR_URL env var (the repo secret in CI)
#   2. this file, committed at the repo root
#
# (2) exists because a tunnel URL is not a secret and a fine-grained PAT without
# "Secrets: write" cannot set (1). It lets the push-triggered chain be exercised
# without repo-admin access. Prefer the secret in production: it survives a
# tunnel restart without a commit.
ORCHESTRATOR_URL_FILE = ".orchestrator-url"
REVIEWER_NOTE = (
    "> **Reviewer note.** `consumer/test_contract.py` is the fenced verification "
    "target. The agent is forbidden from editing it. **If this PR touches that "
    "file, reject it** — the fix would only be passing because the test changed."
)


def _post_drift(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(f"{base_url}/drift", json=payload, timeout=60.0)
    response.raise_for_status()
    return response.json()


def _poll(base_url: str, run_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/runs/{run_id}", timeout=30.0)
            response.raise_for_status()
            last = response.json()
        except Exception as exc:
            print(f"poll error (retrying): {exc}", file=sys.stderr)
            time.sleep(POLL_SECONDS)
            continue

        print(f"  [{int(time.time() % 10000)}] status={last.get('status')} stage={last.get('stage')}")
        if last.get("is_terminal"):
            return last
        time.sleep(POLL_SECONDS)

    last["status"] = last.get("status") or "error"
    last["error"] = (
        f"Orchestrator did not reach a terminal state within {timeout}s. "
        + (last.get("error") or "")
    ).strip()
    return last


def _pr_body(run: dict[str, Any], base_url: str, run_url: str) -> str:
    drift = run.get("drift") or {}
    provider = drift.get("provider") or {}
    verified = run.get("status") == "verified"

    lines = [
        "## Third-party API drift — automated fix"
        if verified
        else "## Third-party API drift — NEEDS HUMAN",
        "",
        f"**Provider:** {provider.get('title')} `{provider.get('base_version')}` → "
        f"`{provider.get('revision_version')}`",
        f"**Detected by:** {drift.get('tool')}",
        "",
        f"{drift.get('summary', '')}",
        "",
    ]

    if run.get("simulated"):
        lines += [
            "> ⚠️ **Simulated run.** The orchestrator was in DEMO_MODE, so no agent "
            "session ran and no code was changed. Nothing below describes real work.",
            "",
        ]

    lines += ["### Verification", ""]

    if verified:
        attempts = run.get("attempts") or 1
        lines += [
            f"`consumer/test_contract.py` **passed** "
            f"({attempts} test run{'s' if attempts != 1 else ''}).",
            "",
            "This branch was not created until that test passed. The test was written "
            "before the agent existed and the agent cannot modify it.",
        ]
    else:
        lines += [
            "**The contract test did not pass.** This PR is open for visibility only — "
            "do not merge it as-is.",
            "",
            f"Attempts: {run.get('attempts') or 0}",
        ]
        if run.get("error"):
            lines += ["", f"Reported problem: {run['error']}"]

    if run.get("root_cause"):
        lines += ["", "### What the provider changed", "", run["root_cause"]]
    if run.get("fix"):
        lines += ["", "### What changed here", "", run["fix"]]

    files = run.get("files_changed") or []
    if files:
        lines += ["", "Files touched: " + ", ".join(f"`{f}`" for f in files)]

    if run.get("test_output_tail"):
        lines += ["", "<details><summary>Test output</summary>", "",
                  "```", run["test_output_tail"], "```", "", "</details>"]

    breaking = drift.get("breaking") or []
    if breaking:
        lines += ["", "<details><summary>Breaking changes detected</summary>", ""]
        for change in breaking:
            described = (change.get("documentation") or {}).get("described")
            lines.append(
                f"- `{change.get('operation')}` {change.get('location')} — "
                f"{change.get('detail')}"
                + ("" if described else "  _(no vendor description)_")
            )
        lines += ["", "</details>"]

    if run.get("code_diff"):
        lines += ["", "<details><summary>Agent's diff</summary>", "",
                  "```diff", run["code_diff"][:8000], "```", "", "</details>"]

    lines += ["", "### Briefing", ""]
    if run.get("spoken_summary"):
        lines.append(f"> {run['spoken_summary']}")
    if run.get("audio_url"):
        lines += [
            "",
            f"[Play the spoken briefing]({base_url}{run['audio_url']}) "
            "(tunnel URL — live only while the orchestrator is running)",
        ]

    lines += [
        "",
        "---",
        "",
        REVIEWER_NOTE,
        "",
        f"Run `{run.get('id')}` · branch `{run.get('branch')}`"
        + (f" · [live view]({run_url})" if run_url else "")
        + (f" · [Claude Console]({run['console_url']})" if run.get("console_url") else ""),
    ]
    return "\n".join(lines)


def _write_outputs(**values: Any) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        for key, value in values.items():
            print(f"[output] {key}={value}")
        return
    with open(path, "a") as fh:
        for key, value in values.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            fh.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drift", required=True, help="envelope from scripts.detect_drift")
    parser.add_argument("--branch", required=True, help="deterministic branch name")
    parser.add_argument("--base-url", default=os.getenv("ORCHESTRATOR_URL", ""))
    parser.add_argument("--body-out", default="/tmp/pr_body.md")
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args(argv)

    base_url = (args.base_url or "").strip().rstrip("/")
    source = "ORCHESTRATOR_URL secret/env"
    if not base_url and os.path.exists(ORCHESTRATOR_URL_FILE):
        with open(ORCHESTRATOR_URL_FILE) as fh:
            base_url = fh.read().strip().rstrip("/")
        source = ORCHESTRATOR_URL_FILE
    if not base_url:
        print(
            "No orchestrator URL. Set the ORCHESTRATOR_URL repo secret, or commit "
            f"the URL to {ORCHESTRATOR_URL_FILE}.",
            file=sys.stderr,
        )
        return 2
    print(f"orchestrator: {base_url}  (from {source})")

    with open(args.drift) as fh:
        drift = json.load(fh)

    if not (drift.get("breaking") or []):
        print("No breaking changes; nothing to adapt.")
        _write_outputs(status="clean", verified=False, pushed=False, branch=args.branch)
        return 0

    payload = {
        "branch": args.branch,
        "drift": drift,
        "repo": (
            f"https://github.com/{os.getenv('GITHUB_REPOSITORY')}"
            if os.getenv("GITHUB_REPOSITORY")
            else ""
        ),
        "commit": os.getenv("GITHUB_SHA", ""),
        "provider_version": os.getenv("PROVIDER_VERSION", ""),
        # Lets the orchestrator wait for this PR before posting to Slack.
        "expect_pr": True,
    }

    print(f"POST {base_url}/drift  branch={args.branch}")
    started = _post_drift(base_url, payload)
    run_id = started["run_id"]
    run_url = f"{base_url}/?run={run_id}"
    print(f"run {run_id} started — {run_url}")

    run = _poll(base_url, run_id, args.timeout)
    status = run.get("status", "error")
    verified = status == "verified"
    pushed = bool(run.get("pushed"))

    with open(args.body_out, "w") as fh:
        fh.write(_pr_body(run, base_url, run_url) + "\n")

    _write_outputs(
        run_id=run_id,
        status=status,
        verified=verified,
        pushed=pushed,
        branch=run.get("branch") or args.branch,
        run_url=run_url,
    )

    print(f"\nverdict: {status} (pushed={pushed})")
    if not verified:
        print(f"reason: {run.get('error')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
