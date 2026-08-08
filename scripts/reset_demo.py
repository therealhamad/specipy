"""Put the repo back into the pre-drift state and prove the baseline is correct.

Run between rehearsals:

    make reset-demo

Does four things, and fails loudly rather than half-working:

  1. restores tracked files under consumer/ to their committed state, undoing
     any fix a previous rehearsal left behind
  2. asserts the contract test is red on v2 and green on v1 — the state the
     whole demo depends on
  3. (re)starts the provider on :8001 serving v1
  4. asserts the consumer dashboard reads the correct baseline

If a fix has been merged into the branch you're on, step 2 fails and says so,
because at that point restoring from HEAD cannot get you back to broken.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
UVICORN = os.path.join(REPO, ".venv", "bin", "uvicorn")
PROVIDER_PORT = 8001

# The contract test's shape when the consumer is un-fixed: v2 cases fail,
# v1 cases pass.
EXPECTED_FAILED = 4
EXPECTED_PASSED = 16


def step(n: int, text: str) -> None:
    print(f"\n[{n}/4] {text}")


def fail(message: str) -> None:
    print(f"\nFAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def restore_consumer() -> None:
    step(1, "Restoring consumer/ to its committed state")
    dirty = subprocess.run(
        ["git", "-C", REPO, "status", "--porcelain", "--", "consumer"],
        capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        print("  discarding local changes:")
        for line in dirty.splitlines():
            print(f"    {line}")
    else:
        print("  consumer/ already matches HEAD")
    subprocess.run(["git", "-C", REPO, "checkout", "--", "consumer"], check=True)


def check_contract_test() -> None:
    step(2, "Confirming the contract test is red on v2, green on v1")
    # pytest.ini already sets -q; adding another would suppress the count line.
    proc = subprocess.run(
        [PY, "-m", "pytest", "consumer/test_contract.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    # Scan the whole output: with failures, the count line is not the last line.
    match = re.search(r"(\d+) failed,\s*(\d+) passed", proc.stdout)
    if not match:
        if re.search(r"\b(\d+) passed\b", proc.stdout) and "failed" not in proc.stdout:
            fail(
                "the contract test is fully green, so the consumer is already "
                "fixed on this branch. A previous fix was merged. Revert "
                "consumer/normalize.py on this branch before rehearsing."
            )
        print(proc.stdout[-1200:])
        fail("could not find a pass/fail count in the pytest output")

    failed, passed = int(match.group(1)), int(match.group(2))
    print(f"  {failed} failed, {passed} passed")
    if (failed, passed) != (EXPECTED_FAILED, EXPECTED_PASSED):
        fail(
            f"expected {EXPECTED_FAILED} failed / {EXPECTED_PASSED} passed "
            f"(all v2 cases red, all v1 cases green), got {failed}/{passed}"
        )
    print("  correct: the drift is present and unfixed")


def _kill_existing() -> None:
    subprocess.run(["pkill", "-f", "uvicorn provider.app"], capture_output=True)
    time.sleep(1)


def start_provider_v1() -> subprocess.Popen:
    step(3, f"Starting the provider on :{PROVIDER_PORT} serving v1")
    _kill_existing()
    env = {**os.environ, "PROVIDER_VERSION": "v1"}
    log = open("/tmp/provider_v1.log", "wb")
    proc = subprocess.Popen(
        [UVICORN, "provider.app:app", "--port", str(PROVIDER_PORT), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=log, stderr=log,
        start_new_session=True,
    )
    import httpx

    for _ in range(30):
        try:
            health = httpx.get(f"http://127.0.0.1:{PROVIDER_PORT}/health", timeout=3).json()
            if health.get("provider_version") == "v1":
                print(f"  up, serving {health['provider_version']} (pid {proc.pid})")
                return proc
        except Exception:
            time.sleep(0.5)
    fail("provider did not come up on v1; see /tmp/provider_v1.log")


def check_baseline() -> None:
    step(4, "Confirming the consumer reads the correct baseline")
    os.environ["PROVIDER_URL"] = f"http://127.0.0.1:{PROVIDER_PORT}"
    # Imported here so PROVIDER_URL is read after it is set.
    sys.path.insert(0, REPO)
    from consumer.client import fetch_payments
    from consumer.normalize import normalize_payment, settled_total_cents

    raw = fetch_payments()
    payments = [normalize_payment(row) for row in raw]
    total = settled_total_cents(payments)
    statuses = [p["status"] for p in payments]

    # Derive what correct looks like from the payload the provider actually
    # served, rather than a hardcoded figure. A constant here goes stale the
    # moment the vendor's data changes, and then reports a false failure.
    expected_statuses = [row.get("transaction_status") for row in raw]
    expected_total = sum(
        row["amount_cents"] for row in raw if row.get("transaction_status") == "completed"
    )

    print(f"  recognised revenue: ${total / 100:,.2f}  ({len(raw)} payments)")
    print(f"  statuses:           {statuses}")

    if None in expected_statuses:
        fail("provider is not serving v1 — payload has no 'transaction_status'")
    if "unknown" in statuses:
        fail("statuses contain 'unknown' — the consumer is misreading the payload")
    if statuses != expected_statuses:
        fail(f"normalisation is lossy: expected {expected_statuses}, got {statuses}")
    if total != expected_total:
        fail(f"revenue should be ${expected_total / 100:,.2f}, got ${total / 100:,.2f}")
    if total <= 0:
        fail("revenue is zero — nothing is being recognised as completed")
    print("  correct: every status matches the provider's own value, no 'unknown'")


def main() -> int:
    print("Resetting the demo to its pre-drift baseline")
    restore_consumer()
    check_contract_test()
    start_provider_v1()
    check_baseline()
    print(
        "\nReady to rehearse.\n"
        f"  provider  : http://127.0.0.1:{PROVIDER_PORT}  (v1, running in the background)\n"
        "  consumer  : make consumer      -> http://127.0.0.1:8002\n"
        "  ship v2   : commit any change under provider/ and push\n"
        "  stop      : pkill -f 'uvicorn provider.app'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
