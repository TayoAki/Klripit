"""Shared-secret gate for a self-hosted instance that is reachable from the internet.

Self-host (BILLING off) has no users and no auth: every endpoint, ``/mcp``
included, runs on the server's own ``GEMINI_API_KEY`` / ``UPLOAD_POST_API_KEY``.
That is right on a laptop and wrong on a public URL (Railway, a VPS), where
anyone who finds the host can spend those keys and post to the connected
accounts. Setting ``OPENSHORTS_ACCESS_TOKEN`` turns this middleware on: every
request must carry ``Authorization: Bearer <token>`` or ``X-API-Key: <token>``,
the same headers an MCP client already sends to the cloud edition, so

    claude mcp add --transport http openshorts https://<host>/mcp \
        --header "Authorization: Bearer <token>"

is all a client needs. The MCP tools call back into the app in-process with the
caller's headers forwarded (``mcp_server._client``), so they pass the gate the
same way the outer request did.

Cloud mode (``BILLING_ENABLED``) ignores the variable: it has real per-user auth.

Left open on purpose:

- ``/health`` and ``/health/ready``: the platform health check sends no header.
- ``OPTIONS``: CORS preflights never carry credentials.
- ``PUT /api/uploads/<uuid>``: the id is a uuid4 minted by an authenticated
  ``POST /api/uploads``, and its URL is what ``create_upload`` tells the agent
  to ``curl -T`` the file to. The slot is the capability.
- ``GET``/``HEAD`` on ``/videos/...`` and ``/thumbnails/...`` with a valid
  ``?exp=&sig=`` path capability (``media_auth``). The MCP tools and webhooks
  hand those out (``sign``) so a clip link opens in a browser with no header.
"""
import hashlib
import hmac
import os
import re
from typing import Optional
from urllib.parse import parse_qs, quote

from starlette.responses import JSONResponse

import media_auth

_OPEN_PATHS = frozenset({"/health", "/health/ready"})
_UPLOAD_PUT = re.compile(
    r"^/api/uploads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _billing_enabled() -> bool:
    return os.environ.get("BILLING_ENABLED", "").lower() in ("1", "true", "yes")


def access_token() -> str:
    """The configured token, or "" when the gate is off (unset, or cloud mode).

    Read per call rather than frozen at import, so tests and a restart with a
    rotated token need nothing special."""
    if _billing_enabled():
        return ""
    return os.environ.get("OPENSHORTS_ACCESS_TOKEN", "").strip()


def media_secret(token: Optional[str] = None) -> str:
    """HMAC key for media path capabilities, derived from the access token.

    Derived rather than the token itself, so a signature in a URL (logs,
    referrers) is never computed with the credential that unlocks the API."""
    token = access_token() if token is None else token
    if not token:
        return ""
    return hashlib.sha256(f"openshorts-media:{token}".encode()).hexdigest()


def sign(url_path: str) -> str:
    """``/videos/<job>/<file>`` with ``?exp=&sig=`` appended when the gate is on.

    Unchanged when the gate is off or the path is not a media path. The path
    is signed as the app stores it (raw filename) and percent-encoded in the
    returned URL: the server decodes it back before verifying, and a raw space
    or non-ASCII title in a URL breaks ``curl`` and some MCP hosts."""
    signed = media_auth.sign_media_url(url_path, media_secret())
    if signed == url_path:
        return url_path
    path, _, query = signed.partition("?")
    return f"{quote(path, safe='/')}?{query}"


def _presented(headers: dict) -> str:
    auth = headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return headers.get("x-api-key", "").strip()


def _token_matches(presented: str, token: str) -> bool:
    return bool(presented) and hmac.compare_digest(presented.encode(), token.encode())


def _signed_media(path: str, query_string: bytes, token: str) -> bool:
    rel = media_auth.strip_media_prefix(path)
    if rel is None:
        return False
    qs = parse_qs(query_string.decode("latin-1"))
    exp = (qs.get("exp") or [""])[0]
    sig = (qs.get("sig") or [""])[0]
    return media_auth.verify_path_signature(rel, exp, sig, media_secret(token))


def allowed(scope, token: str) -> bool:
    """Whether this HTTP request may reach the app while the gate is on."""
    method = scope.get("method", "GET")
    path = scope.get("path", "")
    if method == "OPTIONS" or path in _OPEN_PATHS:
        return True
    if method == "PUT" and _UPLOAD_PUT.match(path):
        return True
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    if _token_matches(_presented(headers), token):
        return True
    if method in ("GET", "HEAD"):
        return _signed_media(path, scope.get("query_string", b""), token)
    return False


class AccessGateMiddleware:
    """Pure ASGI, so streamed bodies (uploads, range downloads) pass untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = access_token()
        if not token or allowed(scope, token):
            return await self.app(scope, receive, send)
        response = JSONResponse(
            {"error": "Authentication required: send Authorization: Bearer "
                      "<OPENSHORTS_ACCESS_TOKEN> (or X-API-Key) with every request."},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="openshorts"'},
        )
        await response(scope, receive, send)
