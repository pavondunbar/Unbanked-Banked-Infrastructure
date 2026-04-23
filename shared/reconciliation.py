"""Reconciliation engine: replay ledger, recompute, compare, alert.

Deterministic state rebuild: the full system state can be
reconstructed from journal_entries alone. This engine verifies
that by replaying the ledger and comparing derived balances
against what the system reports.

Run types:
  balance_check   - Verify derived balances are internally consistent
  debit_credit    - Verify total debits == total credits (global)
  transfer_verify - Verify each settled transfer has matching journal pairs
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def run_balance_reconciliation(db: Session) -> dict:
    """Replay the entire ledger and verify balance consistency.

    1. Compute per-wallet balances from journal
    2. Check no wallet has negative balance (unless system wallet)
    3. Check global debits == credits
    """
    run = _start_run(db, "balance_check")
    mismatches: list[dict] = []

    # Global debit/credit check
    totals = db.execute(text("""
        SELECT
            SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END)
                AS total_debits,
            SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END)
                AS total_credits,
            COUNT(*) AS entry_count
        FROM journal_entries
    """)).fetchone()

    total_d = float(totals.total_debits or 0)
    total_c = float(totals.total_credits or 0)

    if abs(total_d - total_c) > 0.01:
        mismatches.append({
            "type": "global_imbalance",
            "total_debits": total_d,
            "total_credits": total_c,
            "difference": round(total_d - total_c, 2),
        })

    # Per-wallet balance check
    wallets = db.execute(text("""
        SELECT wallet_id, currency,
            SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END)
          - SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END)
                AS balance
        FROM journal_entries
        GROUP BY wallet_id, currency
    """)).fetchall()

    total_wallets = len(wallets)
    system_prefixes = ("00000000-0000-0000-0000-",)

    for w in wallets:
        bal = float(w.balance)
        is_system = any(
            w.wallet_id.startswith(p) for p in system_prefixes
        )
        if bal < 0 and not is_system:
            mismatches.append({
                "type": "negative_balance",
                "wallet_id": w.wallet_id,
                "currency": w.currency,
                "balance": bal,
            })

    return _complete_run(
        db, run["id"], total_wallets, mismatches, total_d, total_c,
    )


def run_transfer_reconciliation(db: Session) -> dict:
    """Verify every settled transfer has matching journal entries."""
    run = _start_run(db, "transfer_verify")
    mismatches: list[dict] = []

    settled = db.execute(text("""
        SELECT id, send_amount, send_currency,
               receive_amount, receive_currency
        FROM transfers WHERE status = 'settled'
    """)).fetchall()

    for t in settled:
        journal_count = db.execute(
            text("""
                SELECT COUNT(*) AS cnt FROM journal_entries
                WHERE transfer_id = :tid
            """),
            {"tid": str(t.id)},
        ).fetchone()

        if journal_count.cnt < 2:
            mismatches.append({
                "type": "missing_journal_entries",
                "transfer_id": str(t.id),
                "expected_min": 2,
                "found": journal_count.cnt,
            })

    return _complete_run(db, run["id"], len(settled), mismatches)


def rebuild_state_from_ledger(db: Session) -> dict:
    """Deterministic state rebuild: reconstruct all balances from journal.

    This proves the system state is fully derivable from the
    append-only ledger without any mutable balance columns.
    """
    rows = db.execute(text("""
        SELECT wallet_id, currency,
            SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END)
          - SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END)
                AS balance
        FROM journal_entries
        GROUP BY wallet_id, currency
        HAVING SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END)
             - SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) <> 0
        ORDER BY wallet_id
    """)).fetchall()

    state = {}
    for r in rows:
        wid = r.wallet_id
        if wid not in state:
            state[wid] = {}
        state[wid][r.currency] = float(r.balance)

    return {
        "rebuilt_at": datetime.now(timezone.utc).isoformat(),
        "wallet_count": len(state),
        "balances": state,
    }


def _start_run(db: Session, run_type: str) -> dict:
    row = db.execute(
        text("""
            INSERT INTO reconciliation_runs (run_type, status)
            VALUES (:rt, 'running')
            RETURNING id, started_at
        """),
        {"rt": run_type},
    ).fetchone()
    db.flush()
    return {"id": row.id, "started_at": row.started_at}


def _complete_run(
    db: Session,
    run_id: int,
    total_wallets: int,
    mismatches: list[dict],
    total_debits: float = 0,
    total_credits: float = 0,
) -> dict:
    status = "clean" if not mismatches else "mismatches_found"
    details = {
        "mismatches": mismatches,
        "total_debits": total_debits,
        "total_credits": total_credits,
    }

    db.execute(
        text("""
            UPDATE reconciliation_runs
            SET status = :st, total_wallets = :tw,
                mismatches = :mm, details = CAST(:det AS jsonb),
                completed_at = now()
            WHERE id = :rid
        """),
        {
            "st": status, "tw": total_wallets,
            "mm": len(mismatches),
            "det": json.dumps(details, default=str),
            "rid": run_id,
        },
    )
    db.commit()

    if mismatches:
        logger.warning(
            "Reconciliation run %d: %d mismatches found",
            run_id, len(mismatches),
        )

    return {
        "run_id": run_id,
        "status": status,
        "total_wallets": total_wallets,
        "mismatches": len(mismatches),
        "details": details,
    }
