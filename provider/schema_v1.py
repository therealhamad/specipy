"""v1 response schema — fully documented, the way a spec is supposed to look."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Payment(BaseModel):
    id: str = Field(..., description="Unique identifier for the payment.")
    amount_cents: int = Field(
        ..., description="Amount charged, in the smallest unit of the currency."
    )
    currency: str = Field(..., description="ISO-4217 currency code, lowercase.")
    transaction_status: Literal["completed", "pending", "failed"] = Field(
        ...,
        description=(
            "Lifecycle state of the transaction. 'completed' means funds have "
            "been captured and the payment can be recognised as revenue."
        ),
    )
    created_at: datetime = Field(..., description="When the payment was created.")
