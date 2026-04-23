"""Request context propagation for audit trails.

Every state transition records request_id, actor_id, and actor_service.
These are set at the API gateway and propagated via HTTP headers.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import Request

request_id_var: ContextVar[uuid.UUID | None] = ContextVar(
    "request_id", default=None,
)
actor_id_var: ContextVar[str | None] = ContextVar(
    "actor_id", default=None,
)
actor_service_var: ContextVar[str | None] = ContextVar(
    "actor_service", default=None,
)


@dataclass(frozen=True)
class RequestContext:
    request_id: uuid.UUID | None
    actor_id: str | None
    actor_service: str | None


def extract_context(request: Request) -> RequestContext:
    """Extract trace context from incoming request headers."""
    raw_id = request.headers.get("X-Request-ID")
    rid = None
    if raw_id:
        try:
            rid = uuid.UUID(raw_id)
        except ValueError:
            rid = None

    ctx = RequestContext(
        request_id=rid or uuid.uuid4(),
        actor_id=request.headers.get("X-Actor-ID"),
        actor_service=request.headers.get("X-Actor-Service"),
    )
    request_id_var.set(ctx.request_id)
    actor_id_var.set(ctx.actor_id)
    actor_service_var.set(ctx.actor_service)
    return ctx


def get_context() -> RequestContext:
    """Get the current request context."""
    return RequestContext(
        request_id=request_id_var.get(),
        actor_id=actor_id_var.get(),
        actor_service=actor_service_var.get(),
    )


def context_headers() -> dict[str, str]:
    """Build headers dict for propagating context to downstream services."""
    ctx = get_context()
    headers: dict[str, str] = {}
    if ctx.request_id:
        headers["X-Request-ID"] = str(ctx.request_id)
    if ctx.actor_id:
        headers["X-Actor-ID"] = ctx.actor_id
    if ctx.actor_service:
        headers["X-Actor-Service"] = ctx.actor_service
    return headers
