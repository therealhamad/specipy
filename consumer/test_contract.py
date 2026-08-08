"""Contract test — the safety artifact this whole system depends on.

=============================== FENCED FILE ===============================
This file is the external verification target. It was written *before* any
fix agent existed, and no agent is permitted to modify it. If a proposed fix
only passes because this file changed, the fix is not a fix.

Reviewers: if a pull request from the drift agent touches this file, reject it.
===========================================================================

The contract is about the *consumer's* internal shape, not the provider's.
``normalize_payment`` is the single seam between "whatever the vendor sent us"
and "the shape the rest of the app relies on". Everything downstream — the
dashboard, the revenue total, any future reporting — reads the normalized dict.

So the contract is:

  * ``status`` is always one of the canonical lowercase strings.
  * a payment that the vendor considers finished-and-captured normalizes to
    ``"completed"``, whatever the vendor happens to call that this month.
  * amounts and currency keep their types.
  * both the v1 and the v2 provider payload shapes are accepted, because a
    fix that breaks v1 has just moved the outage rather than resolved it.

The payloads below are captured verbatim from the provider at each version.
"""

from __future__ import annotations

import pytest

from consumer.normalize import CANONICAL_STATUSES, normalize_payment, settled_total_cents

# --------------------------------------------------------------------------
# Captured provider payloads. v1 = baseline, v2 = after the breaking change.
# --------------------------------------------------------------------------

V1_COMPLETED = {
    "id": "pay_9f21ac",
    "amount_cents": 4200,
    "currency": "usd",
    "transaction_status": "completed",
    "created_at": "2026-08-01T09:14:03Z",
}
V1_PENDING = {
    "id": "pay_3a88d1",
    "amount_cents": 899,
    "currency": "usd",
    "transaction_status": "pending",
    "created_at": "2026-08-06T11:02:19Z",
}
V1_FAILED = {
    "id": "pay_51ee70",
    "amount_cents": 32500,
    "currency": "usd",
    "transaction_status": "failed",
    "created_at": "2026-08-07T08:30:44Z",
}

V2_COMPLETED = {
    "id": "pay_9f21ac",
    "amount_cents": 4200,
    "currency": "usd",
    "status": {"code": "SETTLED", "updated_at": "2026-08-01T09:14:07Z"},
    "settlement_reference": "stl_2026080100881",
    "created_at": "2026-08-01T09:14:03Z",
}
V2_PENDING = {
    "id": "pay_3a88d1",
    "amount_cents": 899,
    "currency": "usd",
    "status": {"code": "AUTH_PENDING", "updated_at": "2026-08-06T11:02:19Z"},
    "settlement_reference": None,
    "created_at": "2026-08-06T11:02:19Z",
}
V2_FAILED = {
    "id": "pay_51ee70",
    "amount_cents": 32500,
    "currency": "usd",
    "status": {"code": "DECLINED", "updated_at": "2026-08-07T08:30:46Z"},
    "settlement_reference": None,
    "created_at": "2026-08-07T08:30:44Z",
}

ALL_PAYLOADS = [
    V1_COMPLETED,
    V1_PENDING,
    V1_FAILED,
    V2_COMPLETED,
    V2_PENDING,
    V2_FAILED,
]


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(V1_COMPLETED, "completed", id="v1-completed"),
        pytest.param(V1_PENDING, "pending", id="v1-pending"),
        pytest.param(V1_FAILED, "failed", id="v1-failed"),
        pytest.param(V2_COMPLETED, "completed", id="v2-settled"),
        pytest.param(V2_PENDING, "pending", id="v2-auth-pending"),
        pytest.param(V2_FAILED, "failed", id="v2-declined"),
    ],
)
def test_status_is_mapped_to_the_canonical_vocabulary(payload, expected):
    """A captured-and-settled payment reads as 'completed' on every version."""
    assert normalize_payment(payload)["status"] == expected


@pytest.mark.parametrize("payload", ALL_PAYLOADS)
def test_status_is_always_a_canonical_string(payload):
    """Never a dict, never None, never a raw vendor enum leaking through."""
    status = normalize_payment(payload)["status"]
    assert isinstance(status, str), f"status must be a string, got {type(status).__name__}"
    assert status in CANONICAL_STATUSES, f"{status!r} is not a canonical status"


@pytest.mark.parametrize("payload", ALL_PAYLOADS)
def test_identity_and_amount_fields_keep_their_shape(payload):
    result = normalize_payment(payload)
    assert result["id"] == payload["id"]
    assert result["amount_cents"] == payload["amount_cents"]
    assert isinstance(result["amount_cents"], int)
    assert result["currency"] == "usd"
    assert result["created_at"] == payload["created_at"]


def test_revenue_total_counts_completed_payments_only():
    """The number a human actually looks at. Silently wrong is the failure mode."""
    for label, batch in (
        ("v1", [V1_COMPLETED, V1_PENDING, V1_FAILED]),
        ("v2", [V2_COMPLETED, V2_PENDING, V2_FAILED]),
    ):
        normalized = [normalize_payment(p) for p in batch]
        assert settled_total_cents(normalized) == 4200, f"{label} revenue total is wrong"


def test_unrecognised_payload_degrades_without_crashing():
    """An unknown future shape must not take the app down."""
    result = normalize_payment({"id": "pay_future", "amount_cents": 100, "currency": "usd"})
    assert result["status"] == "unknown"
    assert result["id"] == "pay_future"
