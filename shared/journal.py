"""Immutable double-entry accounting engine.

Every token movement produces a balanced debit+credit pair.
Balances are DERIVED from journal entries — never stored mutably.
The journal_entries table is protected by Postgres rules that
silently reject UPDATE and DELETE.

Chart of Accounts (COA):
  CLIENT_WALLET         Client token holdings
  OMNIBUS_RESERVE       Platform backing reserve
  FX_POOL_{currency}    FX liquidity per currency
  FEE_REVENUE           Collected fees
  AGENT_FLOAT           Agent cash float
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.context import get_context


def record_journal_pair(
    db: Session,
    debit_wallet_id: str,
    credit_wallet_id: str,
    amount: float,
    currency: str,
    debit_coa: str,
    credit_coa: str,
    description: str,
    transfer_id: str | None = None,
    settlement_id: str | None = None,
) -> str:
    """Record a balanced debit+credit journal entry pair.

    Returns the shared entry_ref linking both sides.
    """
    if amount <= 0:
        raise ValueError(f"Journal amount must be positive, got {amount}")

    entry_ref = f"JRN-{uuid.uuid4().hex[:12]}"
    ctx = get_context()

    for direction, wallet_id, coa in [
        ("debit", debit_wallet_id, debit_coa),
        ("credit", credit_wallet_id, credit_coa),
    ]:
        db.execute(
            text("""
                INSERT INTO journal_entries
                    (entry_ref, wallet_id, direction, amount, currency,
                     coa_code, description, transfer_id, settlement_id,
                     request_id, actor_id, actor_service)
                VALUES
                    (:ref, :wallet, :dir, :amt, :cur,
                     :coa, :desc, :tid, :sid,
                     :rid, :aid, :asvc)
            """),
            {
                "ref": entry_ref, "wallet": wallet_id,
                "dir": direction, "amt": amount, "cur": currency,
                "coa": coa, "desc": description,
                "tid": transfer_id, "sid": settlement_id,
                "rid": str(ctx.request_id) if ctx.request_id else None,
                "aid": ctx.actor_id, "asvc": ctx.actor_service,
            },
        )
    return entry_ref


def get_balance(db: Session, wallet_id: str, currency: str) -> float:
    """Derive balance from journal entries (credits - debits)."""
    row = db.execute(
        text("""
            SELECT
                COALESCE(SUM(CASE WHEN direction='credit'
                             THEN amount ELSE 0 END), 0)
              - COALESCE(SUM(CASE WHEN direction='debit'
                             THEN amount ELSE 0 END), 0) AS balance
            FROM journal_entries
            WHERE wallet_id = :wid AND currency = :cur
        """),
        {"wid": wallet_id, "cur": currency},
    ).fetchone()
    return float(row.balance) if row else 0.0


def get_all_balances(db: Session, wallet_id: str) -> dict[str, float]:
    """Get all currency balances for a wallet."""
    rows = db.execute(
        text("""
            SELECT currency,
                COALESCE(SUM(CASE WHEN direction='credit'
                             THEN amount ELSE 0 END), 0)
              - COALESCE(SUM(CASE WHEN direction='debit'
                             THEN amount ELSE 0 END), 0) AS balance
            FROM journal_entries
            WHERE wallet_id = :wid
            GROUP BY currency
        """),
        {"wid": wallet_id},
    ).fetchall()
    return {r.currency: float(r.balance) for r in rows}


def acquire_balance_lock(db: Session, wallet_id: str, currency: str) -> None:
    """Acquire a PostgreSQL advisory lock to prevent double-spend.

    Lock is held until the transaction commits or rolls back.
    """
    lock_key = hash(f"{wallet_id}:{currency}") & 0x7FFFFFFF
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": lock_key},
    )
