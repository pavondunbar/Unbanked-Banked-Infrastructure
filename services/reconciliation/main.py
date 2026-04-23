"""Reconciliation Service: replay ledger, recompute, compare, alert.

Endpoints:
  POST /reconciliation/balance   - Verify all balances from journal
  POST /reconciliation/transfers - Verify settled transfers have journal pairs
  GET  /reconciliation/rebuild   - Deterministic state rebuild from ledger
  GET  /reconciliation/runs      - List past reconciliation runs
"""

from __future__ import annotations

import sys
sys.path.insert(0, "/app")

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.database import get_db
from shared.reconciliation import (
    rebuild_state_from_ledger,
    run_balance_reconciliation,
    run_transfer_reconciliation,
)

app = FastAPI(title="Reconciliation Service")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "reconciliation"}


@app.post("/reconciliation/balance")
async def balance_recon(db: Session = Depends(get_db)) -> dict:
    """Replay ledger and verify balance consistency.

    Checks:
    1. Global debits == credits
    2. No non-system wallet has negative balance
    """
    return run_balance_reconciliation(db)


@app.post("/reconciliation/transfers")
async def transfer_recon(db: Session = Depends(get_db)) -> dict:
    """Verify every settled transfer has matching journal entries."""
    return run_transfer_reconciliation(db)


@app.get("/reconciliation/rebuild")
async def rebuild(db: Session = Depends(get_db)) -> dict:
    """Deterministic state rebuild from the append-only ledger.

    Reconstructs all wallet balances purely from journal entries,
    proving the system state is fully derivable from the ledger.
    """
    return rebuild_state_from_ledger(db)


@app.get("/reconciliation/runs")
async def list_runs(db: Session = Depends(get_db)) -> list[dict]:
    """List past reconciliation runs."""
    rows = db.execute(text("""
        SELECT id, run_type, status, total_wallets, mismatches,
               started_at, completed_at
        FROM reconciliation_runs
        ORDER BY started_at DESC LIMIT 50
    """)).fetchall()

    return [
        {
            "id": r.id,
            "run_type": r.run_type,
            "status": r.status,
            "total_wallets": r.total_wallets,
            "mismatches": r.mismatches,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in rows
    ]
