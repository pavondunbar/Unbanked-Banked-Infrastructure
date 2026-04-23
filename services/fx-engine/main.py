"""FX Engine: Currency conversion for cross-border transfers.

Manages exchange rates with bid/ask spreads and provides
quote generation for the transfer engine.
"""

from __future__ import annotations

import sys
sys.path.insert(0, "/app")

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.context import extract_context
from shared.database import get_db

app = FastAPI(title="FX Engine")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "fx-engine"}


@app.get("/rates/{base}/{quote}")
async def get_rate(
    base: str,
    quote: str,
    db: Session = Depends(get_db),
) -> dict:
    """Get current exchange rate with bid/ask spread."""
    if base == quote:
        return {"mid": 1.0, "bid": 1.0, "ask": 1.0, "spread_bps": 0}

    row = db.execute(
        text("""
            SELECT mid_rate, spread_bps FROM fx_rates
            WHERE base_currency = :base AND quote_currency = :quote
            AND active = true
            ORDER BY updated_at DESC LIMIT 1
        """),
        {"base": base.upper(), "quote": quote.upper()},
    ).fetchone()

    if not row:
        raise HTTPException(
            404, f"No active rate for {base}/{quote}",
        )

    mid = float(row.mid_rate)
    spread = float(row.spread_bps) / 10000
    return {
        "base": base.upper(),
        "quote": quote.upper(),
        "mid": mid,
        "bid": round(mid * (1 + spread), 8),
        "ask": round(mid * (1 - spread), 8),
        "spread_bps": float(row.spread_bps),
    }


@app.get("/convert/{from_cur}/{to_cur}/{amount}")
async def convert(
    from_cur: str,
    to_cur: str,
    amount: float,
    db: Session = Depends(get_db),
) -> dict:
    """Convert amount between currencies using ask rate."""
    rate_info = await get_rate(from_cur, to_cur, db)
    ask = rate_info["ask"]
    mid = rate_info["mid"]
    converted = round(amount * ask, 2)
    spread_cost = round(amount * (mid - ask), 2)

    return {
        "from_amount": amount,
        "from_currency": from_cur.upper(),
        "to_amount": converted,
        "to_currency": to_cur.upper(),
        "rate_applied": ask,
        "mid_rate": mid,
        "spread_cost": spread_cost,
    }


@app.get("/rates")
async def list_rates(db: Session = Depends(get_db)) -> list[dict]:
    """List all active FX rates."""
    rows = db.execute(
        text("""
            SELECT base_currency, quote_currency, mid_rate,
                   spread_bps, updated_at
            FROM fx_rates WHERE active = true
            ORDER BY base_currency, quote_currency
        """),
    ).fetchall()

    return [
        {
            "base": r.base_currency,
            "quote": r.quote_currency,
            "mid_rate": float(r.mid_rate),
            "spread_bps": float(r.spread_bps),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
