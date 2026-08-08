"""Self-built "vendor" API for the drift demo.

Two versions live side by side, selected by PROVIDER_VERSION, so one codebase
plays both halves of the demo:

    PROVIDER_VERSION=v1 uvicorn provider.app:app --port 8001   # baseline
    PROVIDER_VERSION=v2 uvicorn provider.app:app --port 8001   # breaking change

The v2 schema is deliberately under-documented, the way real vendors ship:

  * ``transaction_status`` is simply gone — no deprecation notice, no sunset
    header, no mention in the changelog-shaped description field.
  * its replacement is called ``status``, which does not announce itself as a
    replacement, is nested one level deeper, and uses an entirely different
    enum vocabulary (SETTLED / AUTH_PENDING / DECLINED).
  * ``status`` carries no ``description`` at all.
  * the one nicely documented addition (``settlement_reference``) is a decoy:
    it is purely additive and is *not* the breaking change.

That combination is the point of the demo. A diff-and-template script can spot
that a field vanished; only a reasoning agent can work out that ``status.code``
is the replacement and that "SETTLED" means what "completed" used to mean.

The response schema is exported under the same component name (``Payment``) in
both versions, because a vendor renaming its schema component would be a much
louder signal than the one we want to test against.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException

PROVIDER_VERSION = os.getenv("PROVIDER_VERSION", "v2").strip().lower()
if PROVIDER_VERSION not in {"v1", "v2"}:
    raise RuntimeError(f"PROVIDER_VERSION must be v1 or v2, got {PROVIDER_VERSION!r}")

IS_V2 = PROVIDER_VERSION == "v2"

if IS_V2:
    from provider.schema_v2 import Payment
else:
    from provider.schema_v1 import Payment


# --------------------------------------------------------------------------
# Internal store. Vendor-side truth; each version projects it differently.
# --------------------------------------------------------------------------

_PAYMENTS: list[dict[str, Any]] = [
    {
        "id": "pay_9f21ac",
        "amount_cents": 4200,
        "currency": "usd",
        "state": "completed",
        "created_at": "2026-08-01T09:14:03Z",
        "updated_at": "2026-08-01T09:14:07Z",
        "settlement_reference": "stl_2026080100881",
    },
    {
        "id": "pay_7c04be",
        "amount_cents": 15990,
        "currency": "usd",
        "state": "completed",
        "created_at": "2026-08-02T17:41:55Z",
        "updated_at": "2026-08-02T17:42:01Z",
        "settlement_reference": "stl_2026080200914",
    },
    {
        "id": "pay_3a88d1",
        "amount_cents": 899,
        "currency": "usd",
        "state": "pending",
        "created_at": "2026-08-06T11:02:19Z",
        "updated_at": "2026-08-06T11:02:19Z",
        "settlement_reference": None,
    },
    {
        "id": "pay_51ee70",
        "amount_cents": 32500,
        "currency": "usd",
        "state": "failed",
        "created_at": "2026-08-07T08:30:44Z",
        "updated_at": "2026-08-07T08:30:46Z",
        "settlement_reference": None,
    },
    {
        "id": "pay_6b19f2",
        "amount_cents": 7450,
        "currency": "usd",
        "state": "completed",
        "created_at": "2026-08-08T07:12:30Z",
        "updated_at": "2026-08-08T07:12:34Z",
        "settlement_reference": "stl_2026080800937",
    },
]

# v1 vocabulary -> v2 vocabulary. Nothing in the v2 spec spells this out.
_V2_CODE_BY_STATE = {
    "completed": "SETTLED",
    "pending": "AUTH_PENDING",
    "failed": "DECLINED",
}


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """Render one stored row in the active version's response shape."""
    base = {
        "id": row["id"],
        "amount_cents": row["amount_cents"],
        "currency": row["currency"],
        "created_at": row["created_at"],
    }
    if not IS_V2:
        return {**base, "transaction_status": row["state"]}
    return {
        **base,
        "status": {
            "code": _V2_CODE_BY_STATE[row["state"]],
            "updated_at": row["updated_at"],
        },
        "settlement_reference": row["settlement_reference"],
    }


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

_NOTES = {
    "v1": "Returns payments with their transaction status.",
    # Mentions the additive field. Says nothing about the removal.
    "v2": (
        "Returns payments. This release also adds settlement references for "
        "cleared payments."
    ),
}

app = FastAPI(
    title="Acme Payments API",
    version="2026-08-01" if IS_V2 else "2026-02-01",
    description=_NOTES[PROVIDER_VERSION],
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe. Also reports which version of the API is being served."""
    return {"status": "ok", "provider_version": PROVIDER_VERSION}


@app.get("/payments", response_model=list[Payment], tags=["payments"])
def list_payments() -> list[dict[str, Any]]:
    """List all payments for the authenticated merchant."""
    return [_project(row) for row in _PAYMENTS]


@app.get("/payments/{payment_id}", response_model=Payment, tags=["payments"])
def get_payment(payment_id: str) -> dict[str, Any]:
    """Retrieve a single payment by id."""
    for row in _PAYMENTS:
        if row["id"] == payment_id:
            return _project(row)
    raise HTTPException(status_code=404, detail="payment not found")
