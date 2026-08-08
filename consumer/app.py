"""The customer-facing app that quietly breaks when the provider ships v2.

Nothing here throws when the contract shifts. The page still renders, the
provider still returns 200, the logs stay clean — the revenue figure just
becomes wrong. That silence is the whole problem this project exists to solve.

    PROVIDER_URL=http://127.0.0.1:8001 uvicorn consumer.app:app --port 8002
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from consumer.client import fetch_payments, provider_health
from consumer.normalize import normalize_payment, settled_total_cents

app = FastAPI(title="Northwind Finance Dashboard")


def _load() -> dict[str, Any]:
    health = provider_health()
    try:
        raw = fetch_payments()
    except Exception as exc:
        return {
            "provider": health,
            "payments": [],
            "revenue_cents": 0,
            "error": str(exc),
        }
    payments = [normalize_payment(row) for row in raw]
    return {
        "provider": health,
        "payments": payments,
        "revenue_cents": settled_total_cents(payments),
        "error": None,
    }


@app.get("/api/payments")
def api_payments() -> dict[str, Any]:
    return _load()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    data = _load()
    revenue = f"${data['revenue_cents'] / 100:,.2f}"
    version = data["provider"].get("provider_version", "?")

    rows = "\n".join(
        f"""      <tr>
        <td class="mono">{p['id']}</td>
        <td class="mono num">${p['amount_cents'] / 100:,.2f}</td>
        <td><span class="pill pill-{p['status']}">{p['status']}</span></td>
        <td class="mono dim">{p['created_at']}</td>
      </tr>"""
        for p in data["payments"]
    )
    banner = (
        f'<div class="err">provider unreachable — {data["error"]}</div>'
        if data["error"]
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Northwind Finance</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; background: #0e1116;
            color: #e6e9ef; margin: 0; padding: 48px 32px; }}
    .wrap {{ max-width: 760px; margin: 0 auto; }}
    h1 {{ font-size: 20px; font-weight: 600; margin: 0 0 4px; }}
    .sub {{ color: #8b94a3; font-size: 13px; margin-bottom: 32px; }}
    .card {{ background: #161b22; border: 1px solid #232a34; border-radius: 10px;
             padding: 24px; margin-bottom: 24px; }}
    .label {{ color: #8b94a3; font-size: 12px; text-transform: uppercase;
              letter-spacing: .06em; margin-bottom: 8px; }}
    .big {{ font-size: 40px; font-weight: 600; letter-spacing: -.02em; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: left; color: #8b94a3; font-size: 12px; font-weight: 500;
          text-transform: uppercase; letter-spacing: .06em;
          padding: 0 0 10px; border-bottom: 1px solid #232a34; }}
    td {{ padding: 12px 0; border-bottom: 1px solid #1b212a; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }}
    .num {{ text-align: right; }}
    th.num {{ text-align: right; }}
    .dim {{ color: #6b7483; }}
    .pill {{ font-size: 12px; padding: 3px 10px; border-radius: 999px;
             background: #232a34; color: #b6bec9; }}
    .pill-completed {{ background: #10331f; color: #5fd48a; }}
    .pill-pending {{ background: #33290f; color: #e3b341; }}
    .pill-failed {{ background: #3a1a1c; color: #f0776c; }}
    .pill-unknown {{ background: #232a34; color: #8b94a3; }}
    .err {{ background: #3a1a1c; color: #f0776c; padding: 12px 16px;
            border-radius: 8px; margin-bottom: 24px; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Northwind Finance</h1>
    <div class="sub">Reading from Acme Payments API &middot; provider {version}</div>
    {banner}
    <div class="card">
      <div class="label">Recognised revenue</div>
      <div class="big">{revenue}</div>
    </div>
    <div class="card">
      <div class="label" style="margin-bottom:16px">Payments</div>
      <table>
        <thead><tr><th>ID</th><th class="num">Amount</th><th>Status</th><th>Created</th></tr></thead>
        <tbody>
{rows}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>"""
