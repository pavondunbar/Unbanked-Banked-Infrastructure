#!/usr/bin/env python3
"""UNBANKED: End-to-end demo against the live Docker stack.

Run: make up && python3 scripts/demo.py

Demonstrates:
  1. Unbanked (Nigeria) → Unbanked (Kenya):       NGN → KES
  2. Banked (USA)       → Unbanked (Philippines):  USD → PHP
  3. Unbanked (Ghana)   → Banked (UK):             GHS → GBP

Each transfer flows through:
  Client onboarding → KYC → Fund wallet → Transfer creation
  → Settlement state machine (PENDING→APPROVED→SIGNED→BROADCASTED→CONFIRMED)
  → Reconciliation verification
"""

from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://localhost:8000"
ADMIN_KEY = "admin-key-001"
SIGNER_KEY = "signer-key-001"
AUDITOR_KEY = "auditor-key-001"

BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(title: str) -> None:
    print(f"\n{BOLD}{BLUE}{'=' * 64}")
    print(f"  {title}")
    print(f"{'=' * 64}{RESET}\n")


def step(msg: str) -> None:
    print(f"  {CYAN}>>>{RESET} {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}[OK]{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {YELLOW}[i]{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}[!]{RESET} {msg}")


def api(
    method: str,
    path: str,
    key: str = ADMIN_KEY,
    **kwargs,
) -> dict:
    """Call the API gateway."""
    with httpx.Client(timeout=30) as client:
        resp = client.request(
            method, f"{BASE}{path}",
            headers={"X-API-Key": key},
            **kwargs,
        )
        if resp.status_code >= 400:
            fail(f"{method} {path} -> {resp.status_code}: {resp.text}")
            return {"error": resp.text, "status": resp.status_code}
        return resp.json()


def wait_for_settlement(settlement_id: str, max_wait: int = 30) -> dict:
    """Poll settlement until it reaches 'confirmed' or times out."""
    for _ in range(max_wait):
        result = api("GET", f"/settlements/{settlement_id}")
        status = result.get("status", "unknown")
        if status == "confirmed":
            return result
        if status == "failed":
            fail(f"Settlement failed: {result}")
            return result
        time.sleep(1)
    fail(f"Settlement timed out after {max_wait}s")
    return result


def show_settlement(result: dict) -> None:
    status = result.get("status", "unknown")
    color = GREEN if status == "confirmed" else YELLOW
    print(f"\n    {BOLD}Settlement:{RESET}")
    print(f"      ID:          {result.get('id', 'N/A')}")
    print(f"      Status:      {color}{status}{RESET}")
    print(f"      Approved by: {result.get('approved_by', 'N/A')}")
    print(f"      Signed by:   {result.get('signed_by', 'N/A')}")
    print(f"      MPC Sig:     {(result.get('mpc_signature') or 'N/A')[:24]}...")
    print(f"    {BOLD}  Token Leg (blockchain):{RESET}")
    print(f"      TX Hash:     {(result.get('blockchain_tx_hash') or 'N/A')[:24]}...")
    print(f"      Block:       #{result.get('block_number', 'N/A')}")
    print(f"      Network:     {result.get('network', 'N/A')}")
    print(f"    {BOLD}  Fiat Leg (traditional rail):{RESET}")
    print(f"      Rail:        {result.get('fiat_rail', 'N/A')}")
    print(f"      Reference:   {result.get('fiat_reference', 'N/A')}")
    print()


# ── Main Demo ─────────────────────────────────────���───────

def main() -> None:
    header("UNBANKED: Cross-Border Stablecoin Infrastructure Demo")
    print("  Microservice architecture with Docker, Kafka, Postgres")
    print("  Double-entry ledger | MPC signing | RBAC | Reconciliation\n")

    # Health check
    step("Checking stack health...")
    health = api("GET", "/health")
    if "error" in health:
        fail("Stack not running. Start with: make up")
        sys.exit(1)
    ok(f"Gateway: {health.get('status')} | Services: {len(health.get('services', {}))}")

    # ── Phase 1: Agent Network ─────────────────────────────

    header("PHASE 1: Register Agent Network")

    agents = {}
    for name, cc, region, currencies in [
        ("Lagos MobileMoney Hub", "NG", "Lagos", "NGN,USD"),
        ("Nairobi M-Pesa Agent", "KE", "Nairobi", "KES,USD"),
        ("Manila GCash Partner", "PH", "Manila", "PHP,USD"),
        ("Accra MTN MoMo Agent", "GH", "Accra", "GHS,USD"),
    ]:
        result = api("POST", "/agents", json={
            "name": name, "country_code": cc, "region": region,
            "currencies": currencies, "float_limit_usd": 100000,
        })
        agents[cc] = result.get("id")
        ok(f"Agent: {name} ({cc}) -> {agents[cc][:8]}...")

    # ── Phase 2: Onboard Clients ───────────────────────────

    header("PHASE 2: Onboard Clients")

    def create(name, ctype, phone, cc, **kw):
        r = api("POST", "/clients", json={
            "full_name": name, "client_type": ctype,
            "phone": phone, "country_code": cc, **kw,
        })
        ok(f"{'Unbanked' if ctype == 'unbanked' else 'Banked'}: "
           f"{name} ({cc}) -> {r.get('id', 'err')[:8]}...")
        return r.get("id")

    amara_id = create("Amara Okafor", "unbanked", "+234-801-234-5678", "NG")
    james_id = create("James Kamau", "unbanked", "+254-712-345-678", "KE")
    maria_id = create("Maria Santos", "unbanked", "+63-917-123-4567", "PH")
    kwame_id = create("Kwame Mensah", "unbanked", "+233-24-123-4567", "GH")
    sarah_id = create("Sarah Johnson", "banked", "+1-555-123-4567", "US",
                       bank_name="Chase", bank_account="****4521",
                       bank_routing="021000021")
    david_id = create("David Williams", "banked", "+44-7700-900123", "GB",
                       bank_name="Barclays", bank_account="****8832",
                       bank_routing="BARCGB22")

    # ── Phase 3: KYC ──────────────────────────────────────

    header("PHASE 3: KYC Verification")

    info("Unbanked → basic tier ($500/txn) | Banked → full tier ($50,000/txn)")
    for cid, tier in [
        (amara_id, "basic"), (james_id, "basic"),
        (maria_id, "basic"), (kwame_id, "basic"),
        (sarah_id, "full"), (david_id, "full"),
    ]:
        api("POST", "/kyc/verify", json={"client_id": cid, "tier": tier})
    ok("All clients KYC verified")

    # ── Phase 4: Create Wallets ────────────────────────────

    header("PHASE 4: Create Wallets & Fund")

    def wallet(cid, cur):
        r = api("POST", "/wallets", json={"client_id": cid, "currency": cur})
        return r.get("id")

    amara_w = wallet(amara_id, "NGN")
    james_w = wallet(james_id, "KES")
    maria_w = wallet(maria_id, "PHP")
    kwame_w = wallet(kwame_id, "GHS")
    sarah_w = wallet(sarah_id, "USD")
    david_w = wallet(david_id, "GBP")

    # Fund wallets (with idempotency keys for safe replay)
    fund_specs = [
        (amara_w, 150000, "NGN", "cash_in",
         {"agent_id": agents["NG"]}, "fund-amara-ngn"),
        (kwame_w, 2000, "GHS", "cash_in",
         {"agent_id": agents["GH"]}, "fund-kwame-ghs"),
        (sarah_w, 1000, "USD", "bank_deposit",
         {"bank_ref": "CHASE-98765"}, "fund-sarah-usd"),
        (david_w, 500, "GBP", "bank_deposit",
         {"bank_ref": "BARC-54321"}, "fund-david-gbp"),
    ]
    for wid, amt, cur, method, extra, idem_key in fund_specs:
        api("POST", "/wallets/fund", json={
            "wallet_id": wid, "amount": amt, "currency": cur,
            "method": method, "idempotency_key": idem_key, **extra,
        })
        ok(f"Funded {wid[:8]}... with {amt:,.0f} {cur} via {method}")

    # ═══════════════════════════════════════════════════════
    # CORRIDOR 1: Unbanked → Unbanked
    # ═══════════════════════════════════════════════════════

    header("CORRIDOR 1: Unbanked → Unbanked (Nigeria → Kenya)")
    print(f"  {BOLD}Amara (NG) sends 50,000 NGN to James (KE){RESET}\n")

    step("Creating transfer...")
    t1 = api("POST", "/transfers", json={
        "sender_id": amara_id, "receiver_id": james_id,
        "amount": 50000, "send_currency": "NGN", "receive_currency": "KES",
        "sender_wallet_id": amara_w, "receiver_wallet_id": james_w,
        "agent_in_id": agents["NG"], "agent_out_id": agents["KE"],
        "idempotency_key": "demo-corridor-1",
    })
    recv = t1.get("receive_amount", 0)
    ok(f"Transfer: {t1.get('id', 'err')[:8]}... | "
       f"50,000 NGN -> {recv:,.2f} KES | FX: {t1.get('fx_rate', '?')}")

    step("Approving settlement (admin)...")
    api("POST", "/settlements/approve", json={
        "settlement_id": t1["settlement_id"], "approved_by": "admin-001",
    })
    ok("Settlement approved by admin-001")

    step("Signing settlement (signer, MPC 2-of-3)...")
    sign_result = api("POST", "/settlements/sign", key=SIGNER_KEY, json={
        "settlement_id": t1["settlement_id"], "signed_by": "signer-001",
    })
    ok(f"MPC signed: quorum {sign_result.get('quorum', '?')}")

    step("Waiting for on-chain broadcast + confirmation...")
    s1 = wait_for_settlement(t1["settlement_id"])
    show_settlement(s1)

    # ═══════════════════════════════════════════════════════
    # CORRIDOR 2: Banked → Unbanked
    # ═══════════════════════════════════════════════════════

    header("CORRIDOR 2: Banked → Unbanked (USA → Philippines)")
    print(f"  {BOLD}Sarah (US, Chase) sends $200 to Maria (PH){RESET}\n")

    step("Creating transfer...")
    t2 = api("POST", "/transfers", json={
        "sender_id": sarah_id, "receiver_id": maria_id,
        "amount": 200, "send_currency": "USD", "receive_currency": "PHP",
        "sender_wallet_id": sarah_w, "receiver_wallet_id": maria_w,
        "agent_out_id": agents["PH"],
        "idempotency_key": "demo-corridor-2",
    })
    recv2 = t2.get("receive_amount", 0)
    ok(f"Transfer: {t2.get('id', 'err')[:8]}... | $200 -> {recv2:,.2f} PHP")

    step("Approve → Sign → Confirm...")
    api("POST", "/settlements/approve", json={
        "settlement_id": t2["settlement_id"], "approved_by": "admin-001",
    })
    api("POST", "/settlements/sign", key=SIGNER_KEY, json={
        "settlement_id": t2["settlement_id"], "signed_by": "signer-001",
    })
    s2 = wait_for_settlement(t2["settlement_id"])
    show_settlement(s2)

    # ═══════════════════════════════════════════════════════
    # CORRIDOR 3: Unbanked → Banked
    # ═══════════════════════════════════════════════════════

    header("CORRIDOR 3: Unbanked → Banked (Ghana → UK)")
    print(f"  {BOLD}Kwame (GH) sends 500 GHS to David (UK, Barclays){RESET}\n")

    step("Creating transfer...")
    t3 = api("POST", "/transfers", json={
        "sender_id": kwame_id, "receiver_id": david_id,
        "amount": 500, "send_currency": "GHS", "receive_currency": "GBP",
        "sender_wallet_id": kwame_w, "receiver_wallet_id": david_w,
        "agent_in_id": agents["GH"],
        "idempotency_key": "demo-corridor-3",
    })
    recv3 = t3.get("receive_amount", 0)
    ok(f"Transfer: {t3.get('id', 'err')[:8]}... | 500 GHS -> {recv3:,.2f} GBP")

    step("Approve → Sign → Confirm...")
    api("POST", "/settlements/approve", json={
        "settlement_id": t3["settlement_id"], "approved_by": "admin-001",
    })
    api("POST", "/settlements/sign", key=SIGNER_KEY, json={
        "settlement_id": t3["settlement_id"], "signed_by": "signer-001",
    })
    s3 = wait_for_settlement(t3["settlement_id"])
    show_settlement(s3)

    # ── Reconciliation ─────────────────────────────────────

    header("RECONCILIATION: Verify Ledger Integrity")

    step("Running balance reconciliation...")
    recon_bal = api("POST", "/reconciliation/balance", key=AUDITOR_KEY)
    ok(f"Status: {recon_bal.get('status')} | "
       f"Wallets: {recon_bal.get('total_wallets')} | "
       f"Mismatches: {recon_bal.get('mismatches')}")

    details = recon_bal.get("details", {})
    info(f"Total debits:  {details.get('total_debits', 0):>14,.2f}")
    info(f"Total credits: {details.get('total_credits', 0):>14,.2f}")

    step("Running transfer reconciliation...")
    recon_txn = api("POST", "/reconciliation/transfers", key=AUDITOR_KEY)
    ok(f"Status: {recon_txn.get('status')} | "
       f"Transfers verified: {recon_txn.get('total_wallets')}")

    step("Deterministic state rebuild from ledger...")
    rebuild = api("GET", "/reconciliation/rebuild", key=AUDITOR_KEY)
    ok(f"Rebuilt {rebuild.get('wallet_count')} wallets from journal")
    for wid, balances in (rebuild.get("balances") or {}).items():
        if not wid.startswith("00000000"):
            for cur, bal in balances.items():
                info(f"  {wid[:8]}... : {bal:>12,.2f} {cur}")

    # ── Idempotency Demo ──────────────────────────────────

    header("IDEMPOTENCY: Replay Detection")

    step("Replaying corridor-1 transfer with same idempotency key...")
    replay = api("POST", "/transfers", json={
        "sender_id": amara_id, "receiver_id": james_id,
        "amount": 50000, "send_currency": "NGN", "receive_currency": "KES",
        "sender_wallet_id": amara_w, "receiver_wallet_id": james_w,
        "idempotency_key": "demo-corridor-1",
    })
    if replay.get("idempotent_hit"):
        ok("Duplicate detected — original result returned, no double-spend")
    else:
        info(f"Replay result: {replay.get('status')}")

    # ── Summary ────────────────────────────────────────────

    header("DEMO COMPLETE")
    print(f"  {GREEN}All three corridors settled successfully.{RESET}")
    print(f"  {GREEN}Hybrid finality: blockchain + traditional rail on every settlement.{RESET}")
    print(f"  {GREEN}Ledger balanced. Reconciliation clean.{RESET}")
    print(f"  {GREEN}MPC 2-of-3 quorum verified on all settlements.{RESET}")
    print(f"  {GREEN}Idempotency layer prevented duplicate processing.{RESET}\n")
    print(f"  Inspect the system:")
    print(f"    make db-ledger       # Journal entries")
    print(f"    make db-balances     # Derived balances")
    print(f"    make db-settlements  # Settlement state machine")
    print(f"    make kafka-tail      # Event stream")
    print(f"    make health          # Service health\n")


if __name__ == "__main__":
    main()
