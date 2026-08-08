"""Thin HTTP client for the provider we don't control."""

from __future__ import annotations

import os
from typing import Any

import httpx

PROVIDER_URL = os.getenv("PROVIDER_URL", "http://127.0.0.1:8001").rstrip("/")


def fetch_payments(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Fetch the raw payment list exactly as the provider returns it."""
    response = httpx.get(f"{PROVIDER_URL}/payments", timeout=timeout)
    response.raise_for_status()
    return response.json()


def provider_health(timeout: float = 3.0) -> dict[str, Any]:
    try:
        response = httpx.get(f"{PROVIDER_URL}/health", timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # provider down is a separate, visible failure
        return {"status": "unreachable", "detail": str(exc)}
