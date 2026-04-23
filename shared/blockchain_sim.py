"""Simulated blockchain recording, fiat rail receipts, and MPC signing.

Every settlement produces BOTH:
  1. Token leg: tx_hash, block_number, block_hash (on-chain receipt)
  2. Fiat leg: rail-specific clearing reference (traditional receipt)

This is the hybrid settlement model -- the token movement and
the fiat settlement are complementary, not alternatives.

For demo purposes these are deterministic simulations.
In production, this would call real L2/sidechain RPCs and
real banking APIs (SWIFT, FedWire, M-Pesa, GCash, etc.).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid


def record_on_chain(payload: dict) -> dict[str, str | int]:
    """Generate a simulated blockchain settlement receipt.

    Returns deterministic tx_hash, block_number, and block_hash
    derived from the payload content.
    """
    payload_bytes = json.dumps(
        payload, sort_keys=True, default=str,
    ).encode()

    tx_hash = "0x" + hashlib.sha256(payload_bytes).hexdigest()
    block_seed = int(time.time()) + hash(tx_hash) % 10000
    block_number = 19_000_000 + (block_seed % 1_000_000)
    block_hash = "0x" + hashlib.sha256(
        str(block_number).encode()
    ).hexdigest()

    return {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "block_hash": block_hash,
        "network": "stablecoin-l2",
        "confirmations": 1,
        "gas_used": 21000,
    }


def record_fiat_rail(
    corridor: str,
    send_currency: str,
    receive_currency: str,
    payload: dict,
) -> dict[str, str]:
    """Generate a simulated traditional rail clearing receipt.

    The rail is chosen based on the corridor and currencies:
      - unbanked corridors: mobile_money (M-Pesa, MTN MoMo, GCash)
      - banked sender:      bank_wire (SWIFT / FedWire / ACH)
      - banked receiver:    bank_wire (SWIFT / FedWire / ACH)

    Returns a deterministic reference in the format of the real rail.
    """
    payload_bytes = json.dumps(
        payload, sort_keys=True, default=str,
    ).encode()
    ref_hash = hashlib.sha256(payload_bytes).hexdigest()[:12].upper()

    mobile_money_networks = {
        "NGN": "OPAY", "KES": "MPESA", "GHS": "MTN-MOMO",
        "PHP": "GCASH", "INR": "UPI", "BRL": "PIX",
    }
    wire_networks = {
        "USD": "FEDWIRE", "EUR": "SEPA", "GBP": "FASTER-PAY",
    }

    if corridor == "unbanked_to_unbanked":
        network = mobile_money_networks.get(receive_currency, "MOBILE")
        rail = "mobile_money"
        reference = f"{network}-{ref_hash}"
    elif corridor == "banked_to_unbanked":
        network = wire_networks.get(send_currency, "SWIFT")
        rail = "bank_wire"
        reference = f"{network}-{ref_hash}"
    elif corridor == "unbanked_to_banked":
        network = wire_networks.get(receive_currency, "SWIFT")
        rail = "bank_wire"
        reference = f"{network}-{ref_hash}"
    else:
        rail = "internal"
        reference = f"INT-{ref_hash}"

    return {
        "rail": rail,
        "reference": reference,
        "network": network if corridor != "internal" else "internal",
        "status": "cleared",
    }


def generate_partial_signature(node_id: str, payload: dict) -> str:
    """Generate a partial MPC signature for a single node.

    Each node produces a deterministic partial based on its
    node_id and the sorted payload.
    """
    data = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{node_id}:{data}".encode()).hexdigest()


def combine_partials(partials: list[str], threshold: int) -> str | None:
    """Combine partial signatures into a threshold signature.

    Requires at least `threshold` valid partials.
    Returns the combined signature or None if threshold not met.
    """
    if len(partials) < threshold:
        return None
    sorted_partials = sorted(partials)
    combined = ":".join(sorted_partials)
    return hashlib.sha256(combined.encode()).hexdigest()


def verify_quorum(
    partials: list[str],
    threshold: int = 2,
    total_nodes: int = 3,
) -> dict[str, bool | int | str | None]:
    """Verify that MPC quorum requirements are met.

    Default: 2-of-3 threshold signature scheme.
    """
    combined = combine_partials(partials, threshold)
    return {
        "quorum_met": combined is not None,
        "partials_received": len(partials),
        "threshold": threshold,
        "total_nodes": total_nodes,
        "combined_signature": combined,
    }
