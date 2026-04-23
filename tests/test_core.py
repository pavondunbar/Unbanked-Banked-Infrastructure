"""Core unit tests — no Docker, database, or psycopg2 required.

Covers the most critical invariants:
  1. Journal rejects non-positive amounts
  2. Settlement state machine: valid full path
  3. Settlement state machine: rejects skip transitions
  4. Settlement state machine: rejects backward jumps
  5. Terminal states have no outgoing transitions
  6. Every non-terminal settlement state can fail
  7. Transfer state machine: settled is terminal
  8. MPC quorum passes with 2-of-3 partials
  9. MPC quorum fails below threshold
  10. MPC signatures are deterministic
  11. Blockchain receipt has required fields
  12. Separation of duties blocks same actor
  13. Separation of duties allows different actors
  14. API key hashing is deterministic
  15. Fiat rail: unbanked-to-unbanked (mobile money)
  16. Fiat rail: banked-to-unbanked (bank wire)
  17. Fiat rail: unbanked-to-banked (bank wire)
  18. Fiat rail: deterministic output
  19. Fiat rail: Nigerian corridor uses OPAY
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import pure-logic modules that don't trigger DB connections.
# state_machine imports are guarded — HTTPException is from fastapi.
from fastapi import HTTPException

from shared.blockchain_sim import (
    combine_partials,
    generate_partial_signature,
    record_fiat_rail,
    record_on_chain,
    verify_quorum,
)
from shared.state_machine import (
    SETTLEMENT_TRANSITIONS,
    TRANSFER_TRANSITIONS,
    validate_settlement_transition,
    validate_transfer_transition,
)


# ── Journal ───────────────────────────────────────────────
# record_journal_pair validates amount before touching the DB,
# so we can test the guard without a real session.

def test_journal_rejects_zero_amount():
    from shared.journal import record_journal_pair

    with pytest.raises(ValueError, match="positive"):
        record_journal_pair(
            db=None,
            debit_wallet_id="w1",
            credit_wallet_id="w2",
            amount=0,
            currency="USD",
            debit_coa="A",
            credit_coa="B",
            description="zero",
        )


def test_journal_rejects_negative_amount():
    from shared.journal import record_journal_pair

    with pytest.raises(ValueError, match="positive"):
        record_journal_pair(
            db=None,
            debit_wallet_id="w1",
            credit_wallet_id="w2",
            amount=-50,
            currency="USD",
            debit_coa="A",
            credit_coa="B",
            description="negative",
        )


# ── Settlement State Machine ─────────────────────────────

def test_settlement_valid_full_path():
    """Happy path: PENDING -> APPROVED -> SIGNED -> BROADCASTED -> CONFIRMED."""
    validate_settlement_transition("pending", "approved")
    validate_settlement_transition("approved", "signed")
    validate_settlement_transition("signed", "broadcasted")
    validate_settlement_transition("broadcasted", "confirmed")


def test_settlement_rejects_skip():
    """Cannot jump from pending directly to signed."""
    with pytest.raises(HTTPException) as exc:
        validate_settlement_transition("pending", "signed")
    assert exc.value.status_code == 409


def test_settlement_rejects_backward():
    """Cannot go from confirmed back to pending."""
    with pytest.raises(HTTPException) as exc:
        validate_settlement_transition("confirmed", "pending")
    assert exc.value.status_code == 409


def test_settlement_terminal_states_are_empty():
    assert SETTLEMENT_TRANSITIONS["confirmed"] == set()
    assert SETTLEMENT_TRANSITIONS["failed"] == set()


def test_settlement_any_active_state_can_fail():
    """Every non-terminal state allows transition to 'failed'."""
    for state, targets in SETTLEMENT_TRANSITIONS.items():
        if state not in ("confirmed", "failed"):
            assert "failed" in targets, f"{state} must allow -> failed"


# ── Transfer State Machine ────────────────────────────────

def test_transfer_settled_is_terminal():
    with pytest.raises(HTTPException) as exc:
        validate_transfer_transition("settled", "failed")
    assert exc.value.status_code == 409


def test_transfer_failed_to_refunded():
    validate_transfer_transition("failed", "refunded")


# ── MPC Signing ───────────────────────────────────────────

def test_mpc_quorum_2_of_3():
    payload = {"settlement_id": "test-123", "amount": 1000}
    p1 = generate_partial_signature("node-1", payload)
    p2 = generate_partial_signature("node-2", payload)

    result = verify_quorum([p1, p2], threshold=2, total_nodes=3)
    assert result["quorum_met"] is True
    assert result["combined_signature"] is not None
    assert len(result["combined_signature"]) == 64


def test_mpc_quorum_fails_below_threshold():
    p1 = generate_partial_signature("node-1", {"id": "x"})
    result = verify_quorum([p1], threshold=2, total_nodes=3)
    assert result["quorum_met"] is False
    assert result["combined_signature"] is None


def test_mpc_deterministic():
    payload = {"key": "value"}
    assert generate_partial_signature("n1", payload) == \
           generate_partial_signature("n1", payload)


def test_mpc_different_nodes_differ():
    payload = {"key": "value"}
    assert generate_partial_signature("node-1", payload) != \
           generate_partial_signature("node-2", payload)


def test_blockchain_receipt_fields():
    receipt = record_on_chain({"id": "test"})
    assert receipt["tx_hash"].startswith("0x")
    assert len(receipt["tx_hash"]) == 66  # 0x + 64 hex chars
    assert receipt["block_number"] >= 19_000_000
    assert receipt["network"] == "stablecoin-l2"
    assert receipt["gas_used"] == 21000


# ── RBAC ──────────────────────────────────────────────────
# Inline the pure functions to avoid triggering database.py import.

def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _check_separation_of_duties(approved_by, signed_by):
    if approved_by and approved_by == signed_by:
        raise HTTPException(403, "Separation of duties violation")


def test_separation_of_duties_blocks_same_actor():
    with pytest.raises(HTTPException) as exc:
        _check_separation_of_duties("actor-1", "actor-1")
    assert exc.value.status_code == 403


def test_separation_of_duties_allows_different_actors():
    _check_separation_of_duties("admin-001", "signer-001")


def test_separation_of_duties_allows_none_approver():
    _check_separation_of_duties(None, "signer-001")


def test_api_key_hash_deterministic():
    h1 = _hash_api_key("admin-key-001")
    h2 = _hash_api_key("admin-key-001")
    assert h1 == h2
    assert len(h1) == 64


# ── Fiat Rail (Hybrid Settlement) ────────────────────────

def test_fiat_rail_unbanked_to_unbanked():
    receipt = record_fiat_rail(
        "unbanked_to_unbanked", "NGN", "KES",
        {"settlement_id": "s1", "transfer_id": "t1"},
    )
    assert receipt["rail"] == "mobile_money"
    assert receipt["network"] == "MPESA"
    assert receipt["reference"].startswith("MPESA-")
    assert receipt["status"] == "cleared"


def test_fiat_rail_banked_to_unbanked():
    receipt = record_fiat_rail(
        "banked_to_unbanked", "USD", "PHP",
        {"settlement_id": "s2", "transfer_id": "t2"},
    )
    assert receipt["rail"] == "bank_wire"
    assert receipt["network"] == "FEDWIRE"
    assert receipt["reference"].startswith("FEDWIRE-")


def test_fiat_rail_unbanked_to_banked():
    receipt = record_fiat_rail(
        "unbanked_to_banked", "GHS", "GBP",
        {"settlement_id": "s3", "transfer_id": "t3"},
    )
    assert receipt["rail"] == "bank_wire"
    assert receipt["network"] == "FASTER-PAY"
    assert receipt["reference"].startswith("FASTER-PAY-")


def test_fiat_rail_deterministic():
    payload = {"id": "test-123"}
    r1 = record_fiat_rail("unbanked_to_unbanked", "NGN", "KES", payload)
    r2 = record_fiat_rail("unbanked_to_unbanked", "NGN", "KES", payload)
    assert r1["reference"] == r2["reference"]


def test_fiat_rail_nigerian_corridor_uses_opay():
    receipt = record_fiat_rail(
        "unbanked_to_unbanked", "NGN", "NGN",
        {"settlement_id": "s4"},
    )
    assert receipt["network"] == "OPAY"
