"""Request middleware: Request-ID, logging, timing, and protocol version."""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)


# Bump this whenever the SSE event schema, the JSON envelope, or any
# request/response shape changes in a backwards-incompatible way. The
# TUI compares its own constant against the header on first contact and
# warns the user if they drift — without this, a stale npm-installed
# TUI silently mis-parses new server events and the failure mode looks
# like "the server is broken" rather than "your TUI is out of date".
#
# Compatible additions (new optional fields, new event types the TUI
# can ignore) do NOT require a bump. Removing or repurposing existing
# fields, or changing their semantics, does.
PROTOCOL_VERSION = "1"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add a unique request_id to every request and response."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class TimingMiddleware(BaseHTTPMiddleware):
    """Log request duration for observability."""

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000

        logger.info(
            f"{request.method} {request.url.path} - "
            f"{response.status_code} - {duration_ms:.1f}ms"
        )
        response.headers["X-Duration-Ms"] = f"{duration_ms:.1f}"
        return response


class ProtocolVersionMiddleware(BaseHTTPMiddleware):
    """Stamp every response with ``X-Blade-Protocol-Version``.

    Sticking it on every response (rather than just /health or /version)
    means the TUI can pick it up on its very first hit — typically
    ``POST /api/v1/sessions`` during boot — so we surface a mismatch
    before the user has typed anything.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Blade-Protocol-Version"] = PROTOCOL_VERSION
        return response
