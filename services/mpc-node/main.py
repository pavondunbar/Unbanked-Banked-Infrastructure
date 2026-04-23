"""MPC Node: Threshold signature participant (2-of-3 quorum).

Each node produces a deterministic partial signature from its
unique NODE_ID and the signing payload. Three nodes run in the
isolated signing network; the signing-gateway aggregates partials.
"""

from __future__ import annotations

import hashlib
import json
import os

from fastapi import FastAPI

app = FastAPI(title="MPC Node")

NODE_ID = os.environ.get("NODE_ID", "mpc-node-0")
NODE_INDEX = int(os.environ.get("NODE_INDEX", "0"))


@app.post("/sign")
async def sign(payload: dict) -> dict:
    """Generate a partial signature for the given payload."""
    data = json.dumps(payload, sort_keys=True, default=str)
    partial = hashlib.sha256(f"{NODE_ID}:{data}".encode()).hexdigest()
    return {
        "node_id": NODE_ID,
        "node_index": NODE_INDEX,
        "partial_signature": partial,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "node_id": NODE_ID, "node_index": NODE_INDEX}
