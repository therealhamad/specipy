"""The seam between the vendor's payload shape and this app's internal shape.

Everything downstream reads the dict this module produces, so this is the one
place that has to change when the provider changes its response format.

The contract this must satisfy lives in consumer/test_contract.py.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# The vocabulary the rest of the app is written against. Downstream code
# compares against these literals, so they must not drift with the vendor.
CANONICAL_STATUSES = ("completed", "pending", "failed", "unknown")


def normalize_payment(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one provider payment into the app's internal representation."""
    return {
        "id": str(raw.get("id", "")),
        "amount_cents": int(raw.get("amount_cents") or 0),
        "currency": str(raw.get("currency", "")).lower(),
        "status": raw.get("transaction_status") or "unknown",
        "created_at": str(raw.get("created_at", "")),
    }


def settled_total_cents(payments: Iterable[Mapping[str, Any]]) -> int:
    """Total value of payments that have actually been captured.

    This is the figure the finance dashboard reports as recognised revenue.
    """
    return sum(p["amount_cents"] for p in payments if p["status"] == "completed")
