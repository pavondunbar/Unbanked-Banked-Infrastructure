"""Role-Based Access Control with separation of duties.

Roles:
  admin     - Full access, approve withdrawals
  operator  - Create transfers, manage clients
  signer    - Sign settlements (MPC quorum participant)
  auditor   - Read-only access to audit trails
  system    - Internal service-to-service (publish events)

Separation of duties: the approver and signer of a settlement
must be different actors.
"""

from __future__ import annotations

import hashlib
from typing import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.database import get_db

ROUTE_ROLES: dict[str, set[str]] = {
    "/transfers": {"admin", "operator"},
    "/clients": {"admin", "operator"},
    "/wallets": {"admin", "operator"},
    "/agents": {"admin", "operator"},
    "/settlements/approve": {"admin"},
    "/settlements/sign": {"signer"},
    "/settlements": {"admin", "operator", "signer"},
    "/fx": {"admin", "operator"},
    "/reconciliation": {"admin", "auditor"},
    "/audit": {"admin", "auditor"},
    "/health": {"admin", "operator", "signer", "auditor", "system"},
}


def hash_api_key(key: str) -> str:
    """SHA-256 hash of a plaintext API key."""
    return hashlib.sha256(key.encode()).hexdigest()


def resolve_api_key(
    db: Session,
    api_key: str,
) -> dict[str, str] | None:
    """Look up an API key and return its role if valid."""
    key_hash = hash_api_key(api_key)
    row = db.execute(
        text("""
            SELECT id, role, description FROM api_keys
            WHERE key_hash = :kh AND active = true
        """),
        {"kh": key_hash},
    ).fetchone()
    if not row:
        return None
    return {"id": str(row.id), "role": row.role, "description": row.description}


def require_role(*allowed_roles: str) -> Callable:
    """FastAPI dependency that enforces role-based access."""
    def dependency(
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, str]:
        api_key = (
            request.headers.get("X-API-Key")
            or request.query_params.get("api_key")
        )
        if not api_key:
            raise HTTPException(401, "Missing API key")

        key_info = resolve_api_key(db, api_key)
        if not key_info:
            raise HTTPException(401, "Invalid API key")

        if key_info["role"] not in allowed_roles:
            raise HTTPException(
                403,
                f"Role '{key_info['role']}' cannot access this endpoint. "
                f"Required: {allowed_roles}",
            )
        return key_info

    return dependency


def check_separation_of_duties(
    approved_by: str | None,
    signed_by: str,
) -> None:
    """Ensure approver and signer are different actors."""
    if approved_by and approved_by == signed_by:
        raise HTTPException(
            403,
            "Separation of duties violation: "
            "approver and signer must be different actors",
        )
