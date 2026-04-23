"""Transfer Engine: clients, wallets, agents, KYC, and transfers.

Handles all three corridors:
  1. Unbanked → Unbanked (cash-in → stablecoin → cash-out)
  2. Banked → Unbanked   (bank deposit → stablecoin → cash-out)
  3. Unbanked → Banked   (cash-in → stablecoin → bank withdrawal)

Every transfer creates a settlement record that progresses through
the deterministic state machine in the settlement service.
"""

from __future__ import annotations

import sys
import uuid
sys.path.insert(0, "/app")

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.context import extract_context, get_context
from shared.database import get_db
from shared.events import transfer_created
from shared.idempotency import (
    check_idempotency,
    check_transfer_idempotency,
    store_idempotency,
)
from shared.journal import (
    acquire_balance_lock,
    get_balance,
    record_journal_pair,
)
from shared.outbox import insert_outbox_event
from shared.state_machine import (
    record_transfer_transition,
    validate_transfer_transition,
)

app = FastAPI(title="Transfer Engine")

SYSTEM_OMNIBUS = "00000000-0000-0000-0000-000000000010"
SYSTEM_FX_POOL = "00000000-0000-0000-0000-000000000011"
SYSTEM_FEES = "00000000-0000-0000-0000-000000000012"
FX_ENGINE_URL = "http://fx-engine:8003"

KYC_LIMITS = {
    "none": 0, "basic": 500, "enhanced": 5000, "full": 50000,
}


# ── Request schemas ───────────────────────────────────────

class CreateClient(BaseModel):
    full_name: str
    client_type: str
    phone: str
    country_code: str
    bank_name: str | None = None
    bank_account: str | None = None
    bank_routing: str | None = None

class CreateWallet(BaseModel):
    client_id: str
    currency: str

class RegisterAgent(BaseModel):
    name: str
    country_code: str
    region: str
    currencies: str
    float_limit_usd: float = 50000.0

class VerifyKYC(BaseModel):
    client_id: str
    tier: str

class FundWallet(BaseModel):
    wallet_id: str
    amount: float
    currency: str
    method: str  # "cash_in" or "bank_deposit"
    agent_id: str | None = None
    bank_ref: str | None = None
    idempotency_key: str | None = None

class CreateTransfer(BaseModel):
    sender_id: str
    receiver_id: str
    amount: float
    send_currency: str
    receive_currency: str
    sender_wallet_id: str
    receiver_wallet_id: str
    agent_in_id: str | None = None
    agent_out_id: str | None = None
    idempotency_key: str | None = None


# ── Endpoints ───────────────────────────────���─────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "transfer-engine"}


@app.post("/clients")
async def create_client(
    body: CreateClient,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    extract_context(request)
    # Idempotent: return existing client if phone already registered
    existing = db.execute(
        text("SELECT id, full_name FROM clients WHERE phone = :phone"),
        {"phone": body.phone},
    ).fetchone()
    if existing:
        return {"id": str(existing.id), "full_name": existing.full_name}

    client_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO clients
                (id, full_name, client_type, phone, country_code,
                 bank_name, bank_account, bank_routing)
            VALUES (:id, :name, :ctype, :phone, :cc,
                    :bn, :ba, :br)
        """),
        {"id": client_id, "name": body.full_name,
         "ctype": body.client_type, "phone": body.phone,
         "cc": body.country_code, "bn": body.bank_name,
         "ba": body.bank_account, "br": body.bank_routing},
    )
    db.commit()
    return {"id": client_id, "full_name": body.full_name}


@app.post("/wallets")
async def create_wallet(
    body: CreateWallet,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    extract_context(request)
    # Idempotent: return existing wallet if client+currency pair exists
    existing = db.execute(
        text("""
            SELECT id FROM wallets
            WHERE client_id = :cid AND currency = :cur
        """),
        {"cid": body.client_id, "cur": body.currency},
    ).fetchone()
    if existing:
        return {"id": str(existing.id), "client_id": body.client_id,
                "currency": body.currency}

    wallet_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO wallets (id, client_id, currency)
            VALUES (:id, :cid, :cur)
        """),
        {"id": wallet_id, "cid": body.client_id, "cur": body.currency},
    )
    db.commit()
    return {"id": wallet_id, "client_id": body.client_id,
            "currency": body.currency}


@app.post("/agents")
async def register_agent(
    body: RegisterAgent,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    extract_context(request)
    # Idempotent: return existing agent if name+country match
    existing = db.execute(
        text("""
            SELECT id, name FROM agents
            WHERE name = :name AND country_code = :cc
        """),
        {"name": body.name, "cc": body.country_code},
    ).fetchone()
    if existing:
        return {"id": str(existing.id), "name": existing.name}

    agent_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO agents
                (id, name, country_code, region, currencies, float_limit_usd)
            VALUES (:id, :name, :cc, :region, :cur, :fl)
        """),
        {"id": agent_id, "name": body.name, "cc": body.country_code,
         "region": body.region, "cur": body.currencies,
         "fl": body.float_limit_usd},
    )
    db.commit()
    return {"id": agent_id, "name": body.name}


@app.post("/kyc/verify")
async def verify_kyc(
    body: VerifyKYC,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    extract_context(request)
    row = db.execute(
        text("SELECT id, kyc_tier FROM clients WHERE id = :id"),
        {"id": body.client_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "Client not found")

    db.execute(
        text("UPDATE clients SET kyc_tier = :tier WHERE id = :id"),
        {"tier": body.tier, "id": body.client_id},
    )
    db.commit()
    return {"client_id": body.client_id, "kyc_tier": body.tier,
            "limit_usd": KYC_LIMITS.get(body.tier, 0)}


@app.post("/wallets/fund")
async def fund_wallet(
    body: FundWallet,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    extract_context(request)

    # Idempotency check
    if body.idempotency_key:
        existing = check_idempotency(db, body.idempotency_key)
        if existing:
            return existing

    description = (
        f"Cash-in via agent {body.agent_id}"
        if body.method == "cash_in"
        else f"Bank deposit ref:{body.bank_ref}"
    )
    entry_ref = record_journal_pair(
        db,
        debit_wallet_id=SYSTEM_OMNIBUS,
        credit_wallet_id=body.wallet_id,
        amount=body.amount,
        currency=body.currency,
        debit_coa="OMNIBUS_RESERVE",
        credit_coa="CLIENT_WALLET",
        description=description,
    )

    result = {"entry_ref": entry_ref, "wallet_id": body.wallet_id,
              "amount": body.amount, "currency": body.currency,
              "method": body.method}

    if body.idempotency_key:
        store_idempotency(db, body.idempotency_key, "/wallets/fund", result)

    db.commit()
    return result


@app.post("/transfers")
async def create_transfer(
    body: CreateTransfer,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Execute a cross-border transfer through the full pipeline."""
    ctx = extract_context(request)

    # Idempotency check
    if body.idempotency_key:
        existing = check_transfer_idempotency(db, body.idempotency_key)
        if existing:
            return existing

    # Load clients
    sender = db.execute(
        text("SELECT * FROM clients WHERE id = :id AND active = true"),
        {"id": body.sender_id},
    ).fetchone()
    receiver = db.execute(
        text("SELECT * FROM clients WHERE id = :id AND active = true"),
        {"id": body.receiver_id},
    ).fetchone()
    if not sender or not receiver:
        raise HTTPException(404, "Sender or receiver not found or inactive")

    # Determine corridor
    corridor = _determine_corridor(sender.client_type, receiver.client_type)

    # FX conversion
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{FX_ENGINE_URL}/convert"
            f"/{body.send_currency}/{body.receive_currency}/{body.amount}",
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"FX conversion failed: {resp.text}")
        fx = resp.json()

    # KYC limit check (convert to USD for comparison)
    amount_usd = body.amount
    if body.send_currency != "USD":
        async with httpx.AsyncClient(timeout=10) as client:
            usd_resp = await client.get(
                f"{FX_ENGINE_URL}/convert/{body.send_currency}/USD/{body.amount}",
            )
            if usd_resp.status_code == 200:
                amount_usd = usd_resp.json()["to_amount"]

    for label, person in [("Sender", sender), ("Receiver", receiver)]:
        limit = KYC_LIMITS.get(person.kyc_tier, 0)
        if amount_usd > limit:
            raise HTTPException(
                403,
                f"{label} KYC tier '{person.kyc_tier}' "
                f"limit ${limit} exceeded (amount: ${amount_usd:.2f})",
            )

    # Create transfer
    transfer_id = str(uuid.uuid4())
    fee_amount = round(body.amount * 0.005, 2)

    db.execute(
        text("""
            INSERT INTO transfers
                (id, corridor, sender_id, receiver_id,
                 send_amount, send_currency,
                 receive_amount, receive_currency,
                 fx_rate, fee_amount, fee_currency,
                 status, agent_in_id, agent_out_id,
                 idempotency_key, request_id)
            VALUES
                (:id, :cor, :sid, :rid,
                 :sa, :sc, :ra, :rc,
                 :fx, :fa, :fc,
                 'pending', :ain, :aout,
                 :ikey, :reqid)
        """),
        {
            "id": transfer_id, "cor": corridor,
            "sid": body.sender_id, "rid": body.receiver_id,
            "sa": body.amount, "sc": body.send_currency,
            "ra": fx["to_amount"], "rc": body.receive_currency,
            "fx": fx["rate_applied"], "fa": fee_amount,
            "fc": body.send_currency,
            "ain": body.agent_in_id, "aout": body.agent_out_id,
            "ikey": body.idempotency_key,
            "reqid": str(ctx.request_id) if ctx.request_id else None,
        },
    )
    record_transfer_transition(db, transfer_id, None, "pending", "Created")

    # Balance check and debit
    acquire_balance_lock(db, body.sender_wallet_id, body.send_currency)
    balance = get_balance(db, body.sender_wallet_id, body.send_currency)
    total_debit = body.amount + fee_amount

    if balance < total_debit:
        _fail_transfer(db, transfer_id, "pending",
                       f"Insufficient balance: {balance:.2f} < {total_debit:.2f}")
        db.commit()
        raise HTTPException(
            400, f"Insufficient balance: {balance:.2f} < {total_debit:.2f}",
        )

    # Debit sender (sell leg)
    record_journal_pair(
        db,
        debit_wallet_id=body.sender_wallet_id,
        credit_wallet_id=SYSTEM_FX_POOL,
        amount=body.amount,
        currency=body.send_currency,
        debit_coa="CLIENT_WALLET",
        credit_coa=f"FX_POOL_{body.send_currency}",
        description=f"Transfer {transfer_id} sell leg",
        transfer_id=transfer_id,
    )

    # Credit receiver (buy leg)
    record_journal_pair(
        db,
        debit_wallet_id=SYSTEM_FX_POOL,
        credit_wallet_id=body.receiver_wallet_id,
        amount=fx["to_amount"],
        currency=body.receive_currency,
        debit_coa=f"FX_POOL_{body.receive_currency}",
        credit_coa="CLIENT_WALLET",
        description=f"Transfer {transfer_id} buy leg",
        transfer_id=transfer_id,
    )

    # Collect fee
    if fee_amount > 0:
        record_journal_pair(
            db,
            debit_wallet_id=body.sender_wallet_id,
            credit_wallet_id=SYSTEM_FEES,
            amount=fee_amount,
            currency=body.send_currency,
            debit_coa="CLIENT_WALLET",
            credit_coa="FEE_REVENUE",
            description=f"Transfer fee {transfer_id}",
            transfer_id=transfer_id,
        )

    # Advance to processing
    validate_transfer_transition("pending", "compliance_check")
    db.execute(
        text("UPDATE transfers SET status = 'compliance_check' WHERE id = :id"),
        {"id": transfer_id},
    )
    record_transfer_transition(
        db, transfer_id, "pending", "compliance_check", "Funds locked",
    )

    validate_transfer_transition("compliance_check", "processing")
    db.execute(
        text("UPDATE transfers SET status = 'processing' WHERE id = :id"),
        {"id": transfer_id},
    )
    record_transfer_transition(
        db, transfer_id, "compliance_check", "processing",
        "Compliance passed (async screening queued)",
    )

    # Create settlement (enters state machine)
    settlement_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO settlements (id, transfer_id, status, request_id)
            VALUES (:id, :tid, 'pending', :rid)
        """),
        {"id": settlement_id, "tid": transfer_id,
         "rid": str(ctx.request_id) if ctx.request_id else None},
    )

    # Publish event via outbox (atomic with ledger writes)
    insert_outbox_event(db, "transfer.created", transfer_created(
        transfer_id=transfer_id,
        corridor=corridor,
        sender_id=body.sender_id,
        receiver_id=body.receiver_id,
        send_amount=body.amount,
        send_currency=body.send_currency,
        receive_amount=fx["to_amount"],
        receive_currency=body.receive_currency,
    ), key=transfer_id)

    db.commit()

    return {
        "id": transfer_id,
        "corridor": corridor,
        "send_amount": body.amount,
        "send_currency": body.send_currency,
        "receive_amount": fx["to_amount"],
        "receive_currency": body.receive_currency,
        "fx_rate": fx["rate_applied"],
        "fee_amount": fee_amount,
        "status": "processing",
        "settlement_id": settlement_id,
    }


@app.get("/wallets/{wallet_id}/balance")
async def wallet_balance(
    wallet_id: str,
    db: Session = Depends(get_db),
) -> dict:
    balances = {}
    rows = db.execute(
        text("""
            SELECT currency,
                SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END)
              - SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END)
                    AS balance
            FROM journal_entries WHERE wallet_id = :wid
            GROUP BY currency
        """),
        {"wid": wallet_id},
    ).fetchall()
    for r in rows:
        balances[r.currency] = float(r.balance)
    return {"wallet_id": wallet_id, "balances": balances}


def _determine_corridor(sender_type: str, receiver_type: str) -> str:
    key = (sender_type, receiver_type)
    corridors = {
        ("unbanked", "unbanked"): "unbanked_to_unbanked",
        ("banked", "unbanked"): "banked_to_unbanked",
        ("unbanked", "banked"): "unbanked_to_banked",
    }
    corridor = corridors.get(key)
    if not corridor:
        raise HTTPException(
            400,
            f"Unsupported corridor: {sender_type} -> {receiver_type}",
        )
    return corridor


def _fail_transfer(
    db: Session, transfer_id: str, current: str, reason: str,
) -> None:
    validate_transfer_transition(current, "failed")
    db.execute(
        text("UPDATE transfers SET status = 'failed' WHERE id = :id"),
        {"id": transfer_id},
    )
    record_transfer_transition(db, transfer_id, current, "failed", reason)
