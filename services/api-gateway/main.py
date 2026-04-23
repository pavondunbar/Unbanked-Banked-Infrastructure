"""API Gateway: sole internet-facing entry point.

Handles:
  - RBAC authentication via API keys
  - Rate limiting (1000 req/60s per key)
  - Request correlation (X-Request-ID generation)
  - Audit logging to Kafka
  - Reverse proxy to internal services
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict

sys.path.insert(0, "/app")

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from sqlalchemy import text

from shared.database import SessionLocal
from shared.rbac import ROUTE_ROLES, resolve_api_key

app = FastAPI(title="UNBANKED API Gateway")
logger = logging.getLogger(__name__)

# Internal service URLs
SERVICES = {
    "transfer-engine": "http://transfer-engine:8001",
    "settlement": "http://settlement:8002",
    "fx-engine": "http://fx-engine:8003",
    "compliance": "http://compliance:8004",
    "reconciliation": "http://reconciliation:8007",
}

# Route prefix → service mapping
ROUTE_MAP: list[tuple[str, str]] = [
    ("/transfers", "transfer-engine"),
    ("/clients", "transfer-engine"),
    ("/wallets", "transfer-engine"),
    ("/agents", "transfer-engine"),
    ("/kyc", "transfer-engine"),
    ("/settlements", "settlement"),
    ("/fx", "fx-engine"),
    ("/rates", "fx-engine"),
    ("/convert", "fx-engine"),
    ("/reconciliation", "reconciliation"),
]

# Rate limiting state
_rate_limits: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 1000
RATE_WINDOW = 60


@app.get("/health")
async def health() -> dict:
    """Aggregate health from all downstream services."""
    results = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for name, url in SERVICES.items():
            try:
                resp = await client.get(f"{url}/health")
                results[name] = resp.json()
            except httpx.HTTPError:
                results[name] = {"status": "unreachable"}

    all_ok = all(
        r.get("status") == "ok" for r in results.values()
    )
    return {"status": "ok" if all_ok else "degraded", "services": results}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy(path: str, request: Request) -> Response:
    """Authenticate, authorize, rate-limit, and proxy requests."""
    request_id = str(uuid.uuid4())
    start = time.time()

    # 1. Authenticate
    api_key = (
        request.headers.get("X-API-Key")
        or request.query_params.get("api_key")
    )
    if not api_key:
        raise HTTPException(401, "Missing API key")

    db = SessionLocal()
    try:
        key_info = resolve_api_key(db, api_key)
    finally:
        db.close()

    if not key_info:
        raise HTTPException(401, "Invalid API key")

    # 2. Rate limit
    now = time.time()
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    _rate_limits[key_hash] = [
        t for t in _rate_limits[key_hash] if t > now - RATE_WINDOW
    ]
    if len(_rate_limits[key_hash]) >= RATE_LIMIT:
        raise HTTPException(429, "Rate limit exceeded")
    _rate_limits[key_hash].append(now)

    # 3. Authorize (match route to required roles)
    role = key_info["role"]
    authorized = False
    for prefix, allowed_roles in ROUTE_ROLES.items():
        if f"/{path}".startswith(prefix):
            if role in allowed_roles:
                authorized = True
            break
    else:
        authorized = role in {"admin", "system"}

    if not authorized:
        raise HTTPException(
            403, f"Role '{role}' not authorized for /{path}",
        )

    # 4. Route to service
    target_service = None
    for prefix, service_name in ROUTE_MAP:
        if f"/{path}".startswith(prefix):
            target_service = SERVICES[service_name]
            break

    if not target_service:
        raise HTTPException(404, f"No service handles /{path}")

    # 5. Forward request with context headers
    target_url = f"{target_service}/{path}"
    headers = {
        "X-Request-ID": request_id,
        "X-Actor-ID": key_info["id"],
        "X-Actor-Service": "api-gateway",
    }

    body = await request.body()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=target_url,
                content=body,
                headers={
                    **headers,
                    "content-type": request.headers.get(
                        "content-type", "application/json",
                    ),
                },
                params=dict(request.query_params),
            )
        except httpx.HTTPError as e:
            logger.error("Proxy error: %s -> %s: %s", path, target_url, e)
            raise HTTPException(502, f"Service unavailable: {e}")

    elapsed = time.time() - start

    # 6. Audit log (best-effort, don't block response)
    _audit_log(request_id, key_info, path, request.method,
               resp.status_code, elapsed)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={
            "X-Request-ID": request_id,
            "Content-Type": resp.headers.get(
                "content-type", "application/json",
            ),
        },
    )


def _audit_log(
    request_id: str,
    key_info: dict,
    path: str,
    method: str,
    status: int,
    elapsed: float,
) -> None:
    """Write audit trail entry (best-effort Kafka publish)."""
    try:
        from shared.kafka_client import publish
        publish("audit.trail", {
            "request_id": request_id,
            "actor_id": key_info["id"],
            "role": key_info["role"],
            "path": f"/{path}",
            "method": method,
            "status_code": status,
            "elapsed_ms": round(elapsed * 1000, 2),
            "timestamp": time.time(),
        })
    except Exception:
        logger.debug("Audit log publish failed (non-critical)")
