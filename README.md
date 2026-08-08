# API Drift Auto-Adapt Engine

Dependabot for the APIs you don't control.

When a third-party provider ships a breaking change to its HTTP API, nothing in
your stack tells you. There's no version bump, no manifest entry, and often no
error — the integration just starts being quietly wrong. Package managers solved
this for declared dependencies because a version number is a machine-readable
signal. Live HTTP contracts have no equivalent.

This is a working end-to-end pipeline that detects the drift, finds the affected
code, fixes it with a Claude Managed Agent, **verifies the fix against a test the
agent is not allowed to modify**, and opens a pull request with a spoken
plain-language briefing.

```
push to provider/**
      │
      ▼
GitHub Actions ── export spec ── oasdiff ──▶ POST /drift ──▶ orchestrator
                                                                  │
                                              SSE ◀───────────────┤
                                               │                  ▼
                                          browser UI      Claude Managed Agent
                                        (simple/technical)   map → fix → verify
                                                                  │
                                                          push branch (git proxy)
                                                                  │
                                            gh pr create ◀────────┘
```

## The change it's tested against

The demo provider's v2 is deliberately *badly* documented, because that's the
realistic case:

| | v1 | v2 |
|---|---|---|
| field | `transaction_status` | `status.code` |
| shape | top-level string | nested object |
| values | `completed` / `pending` / `failed` | `SETTLED` / `AUTH_PENDING` / `DECLINED` |
| description | full prose | **none** |
| deprecation notice | — | **none** |

The only well-documented addition in v2 (`settlement_reference`) is a decoy: it's
purely additive and isn't the breaking change. A diff-and-template script can see
that a field vanished; working out that `status.code` replaces it and that
`SETTLED` means what `completed` used to mean requires reasoning.

The consumer breaks **silently**: `normalize_payment` falls back to `"unknown"`,
so recognised revenue quietly drops to `$0.00` with a 200 response and clean logs.

## Safety model

`consumer/test_contract.py` is the load-bearing artifact. It was written before
any agent existed, it's red on v2 and green on v1, and the agent's system prompt
forbids modifying it by any route — including `conftest.py` hooks, marker
changes, and shadowing the module it imports. The agent only pushes a branch
*after* that test passes, so a wrong fix's worst case is a closed PR: the same
cost as today's status quo, minus the surprise.

The verifier-fail path is implemented, not just planned: bounded retries, then a
PR labelled `needs-human`, or — if the agent never got far enough to push a
branch — a `needs-human` issue instead. Nothing ever claims success it didn't
earn.

## Quick start

```bash
make install
make test          # 4 failures (all v2), 16 passes (all v1) — this is correct
```

Watch it break, with no agent involved:

```bash
make reset-demo    # restores the broken state, starts the provider on v1,
                   # and verifies the baseline. Run this between rehearsals.
make consumer      # :8002  -> revenue $276.40, real statuses
# then, to show the silent breakage:
make provider-v2   # :8001, replacing the v1 process
# reload :8002     -> revenue $0.00, every status "unknown", no error anywhere
```

`make reset-demo` is the one command to run between rehearsals: it discards any
fix a previous run left in `consumer/`, asserts the contract test is red on v2
and green on v1, and checks the dashboard against the provider's own payload
rather than a hardcoded figure.

Run the pipeline with no credentials (simulated, clearly labelled as such):

```bash
make demo          # :8000, then click "Run from sample diff"
```

## Live setup

```bash
cp .env.example .env
make agent-create           # prints AGENT_ID and ENVIRONMENT_ID -> paste into .env
make agent-show             # confirms config and that the prompt fence is present
make voice-check            # one real Maya call; also saves the cached clip
make orchestrator           # :8000
ngrok http 8000             # set the URL as the ORCHESTRATOR_URL repo secret
```

Then push a change under `provider/` and the workflow does the rest.

**Confirm the GitHub PAT is fine-grained with `Contents: Read and write` before
the first run.** The agent pushes at the very end of an otherwise successful
session, so a read-only token wastes a whole run before you find out.

## Layout

| path | what it is |
|---|---|
| `provider/` | the self-built "vendor" API; `PROVIDER_VERSION` selects v1 or v2 |
| `consumer/` | the customer app, plus the fenced `test_contract.py` |
| `orchestrator/` | FastAPI server, CMA session driver, SSE relay, voice |
| `interface/` | dual-mode web UI, one event stream |
| `scripts/` | drift detection, CI bridge, voice verification |
| `specs/` | committed v1 baseline (not fetched at runtime) |

## Two details that would otherwise be bugs

**The SSE stream is opened before the kickoff message is sent.** A CMA session
stream only carries events emitted after it opens, so send-then-stream loses the
opening events and delivers the rest as one buffered lump.

**The read loop doesn't break on a bare `session.status_idle`.** Idle fires
transiently — between parallel tool calls, and whenever the session is waiting on
the client. It's terminal only when `stop_reason.type` isn't `requires_action`.
`session.status_terminated` is always terminal.

## Diff engine

`oasdiff` runs via `docker run tufin/oasdiff`, so CI needs no Go toolchain. A
built-in structural differ always runs alongside it — it carries the per-field
types, enum vocabularies, and the *documentation* signal (whether the vendor
described each changed field) that the agent reasons over. If Docker is
unavailable the built-in differ takes over and the pipeline is unaffected:

```bash
make drift                          # uses Docker if present
DRIFT_FORCE_BUILTIN=1 make drift    # force the fallback
```

## Scope

One provider/consumer pair, one demo vendor. The architecture is
provider-agnostic — the only provider-specific step is obtaining a spec.

Explicitly **not** solved: vendors who ship prose-only changelogs and no spec at
all. This targets the harder-than-clean case (poorly documented but spec-based),
not the hardest case. Auto-merge is also deliberately absent; human review is a
hard requirement.
