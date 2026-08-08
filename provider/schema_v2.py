"""v2 response schema — realistically under-documented.

Everything sparse here is sparse on purpose. See provider/app.py for why.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PaymentStatus(BaseModel):
    code: Literal["SETTLED", "AUTH_PENDING", "DECLINED"]
    updated_at: datetime


class Payment(BaseModel):
    id: str = Field(..., description="Unique identifier for the payment.")
    amount_cents: int = Field(
        ..., description="Amount charged, in the smallest unit of the currency."
    )
    currency: str = Field(..., description="ISO-4217 currency code, lowercase.")
    # No description, and no deprecation notice on the field this replaces.
    # This is the breaking change; the spec gives you nothing to work with.
    status: PaymentStatus
    settlement_reference: Optional[str] = Field(
        None,
        description=(
            "Reference issued by the settlement network once funds have cleared. "
            "Populated for settled payments only; null while a payment is still "
            "awaiting authorisation or if it was declined."
        ),
    )
    created_at: datetime = Field(..., description="When the payment was created.")
