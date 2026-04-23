"""Signing Gateway: MPC coordinator for 2-of-3 threshold signatures.

Fans out signing requests to all MPC nodes in the isolated signing
network, collects partial signatures, and combines them if the
quorum threshold is met.
"""

from __future__ import annotations

import hashlib
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Signing Gateway")
logger = logging.getLogger(__name__)

MPC_NODES = os.environ.get(
    "MPC_NODES",
    "http://mpc-node-1:8010,http://mpc-node-2:8010,http://mpc-node-3:8010",
).split(",")
THRESHOLD = int(os.environ.get("MPC_THRESHOLD", "2"))


@app.post("/sign")
async def sign(payload: dict) -> dict:
    """Coordinate threshold signing across MPC nodes.

    Sends the payload to all nodes, collects partial signatures,
    and combines them if >= threshold partials are received.
    """
    partials: list[dict] = []

    async with httpx.AsyncClient(timeout=10) as client:
        for node_url in MPC_NODES:
            try:
                resp = await client.post(
                    f"{node_url.strip()}/sign", json=payload,
                )
                if resp.status_code == 200:
                    partials.append(resp.json())
            except httpx.HTTPError:
                logger.warning("MPC node unreachable: %s", node_url)

    if len(partials) < THRESHOLD:
        raise HTTPException(
            503,
            f"Quorum not met: {len(partials)}/{THRESHOLD} partials received",
        )

    sorted_sigs = sorted(p["partial_signature"] for p in partials)
    combined = hashlib.sha256(":".join(sorted_sigs).encode()).hexdigest()

    return {
        "combined_signature": combined,
        "partials_received": len(partials),
        "threshold": THRESHOLD,
        "total_nodes": len(MPC_NODES),
        "quorum_met": True,
        "node_details": [
            {"node_id": p["node_id"], "node_index": p["node_index"]}
            for p in partials
        ],
    }


@app.get("/health")
async def health() -> dict:
    node_status = []
    async with httpx.AsyncClient(timeout=3) as client:
        for node_url in MPC_NODES:
            try:
                resp = await client.get(f"{node_url.strip()}/health")
                node_status.append({
                    "url": node_url, "status": "ok",
                    "node_id": resp.json().get("node_id"),
                })
            except httpx.HTTPError:
                node_status.append({"url": node_url, "status": "unreachable"})

    healthy_count = sum(1 for n in node_status if n["status"] == "ok")
    return {
        "status": "ok" if healthy_count >= THRESHOLD else "degraded",
        "nodes": node_status,
        "threshold": THRESHOLD,
        "healthy_nodes": healthy_count,
    }
