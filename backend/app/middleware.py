"""Security middlewares.

Applies the standard HTTP security headers recommended by OWASP and Mozilla's
Observatory for an SPA + JSON API. Each header is conservative for an API/SPA
combo and won't break the React app.

IMPORTANT — implemented as a *pure ASGI* middleware (not Starlette's
BaseHTTPMiddleware). BaseHTTPMiddleware wraps the response via ``call_next`` and
re-streams the body through an anyio memory stream; behind the Cloudflare tunnel
on Railway this deadlocks for streaming/FileResponse/StaticFiles responses
(starlette issue #919), causing every non-trivial route to hang with no first
byte ever emitted. A pure ASGI middleware only mutates the response *headers* in
the ``http.response.start`` message and passes the body through untouched, so it
cannot stall the response. See docs/BACKEND_CHANGES.md.
"""
from __future__ import annotations

import logging

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)


class SecurityHeadersMiddleware:
    """Adds OWASP-recommended security response headers (pure ASGI).

    - HSTS forces HTTPS for one year on Railway (already HTTPS-only).
    - X-Frame-Options + frame-ancestors CSP prevent clickjacking.
    - X-Content-Type-Options stops MIME sniffing.
    - Referrer-Policy limits referrer leak to cross-origin requests.
    - Permissions-Policy disables sensitive browser features we don't use.
    - Cross-Origin-Resource-Policy lets the SPA still fetch from this origin.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = True) -> None:
        self.app = app
        self._hsts = hsts

    @staticmethod
    def _csp_for(path: str) -> str:
        # - /docs, /redoc, /openapi.json: allow CDN for Swagger UI
        # - API paths (/api/, /health, /ws): strict none (JSON only)
        # - Everything else (SPA frontend): allow self for scripts/styles/connect
        if path.startswith("/docs") or path.startswith("/redoc") or path == "/openapi.json":
            return (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        if path.startswith("/api/") or path in ("/health",) or path.startswith("/ws"):
            return (
                "default-src 'none'; "
                "frame-ancestors 'none'; "
                "base-uri 'none'"
            )
        # SPA frontend — allow scripts, styles, fonts, images, and WebSocket
        # connect to self (including ws:// upgrade on the same host).
        return (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss: http://192.168.50.2:8090 http://192.168.50.2:8000 https://example.org https://cam.example.org wss://example.org; "
            "frame-ancestors 'none'; "
            "base-uri 'self'"
        )

    def _is_https(self, scope: Scope) -> bool:
        if scope.get("scheme") == "https":
            return True
        # Behind Railway/Cloudflare the TLS is terminated upstream; trust the
        # forwarded-proto header so HSTS is still emitted for real HTTPS clients.
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-proto" and value.split(b",")[0].strip() == b"https":
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        https = self._is_https(scope)
        csp = self._csp_for(path)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault(
                    "Permissions-Policy",
                    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
                    "magnetometer=(), microphone=(), payment=(), usb=()",
                )
                headers.setdefault("Cross-Origin-Resource-Policy", "cross-origin")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                if self._hsts and https:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                headers.setdefault("Content-Security-Policy", csp)
            await send(message)

        await self.app(scope, receive, send_with_headers)
