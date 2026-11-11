"""Loopback relay using renewable, short-lived gateway access credentials.

Run with ``python -m gateway.relay``. This is a convenience agent, not a
credential-free authentication scheme: its bootstrap credential must be secured.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hmac
import ipaddress
import math
import os
import time
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class RelaySettings:
    central_url: str
    credential: str
    local_token: str
    bind: str = "127.0.0.1"
    port: int = 8090
    allow_remote_bind: bool = False
    max_body_bytes: int = 4 * 1024 * 1024
    request_timeout_seconds: float = 120.0
    journal_directory: str | None = None
    rotation_interval_seconds: int = 86400

    def __post_init__(self) -> None:
        parsed = urlsplit(self.central_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and not _loopback(parsed.hostname))
        ):
            raise ValueError("CENTRAL_GATEWAY_URL must be HTTPS (HTTP only for loopback), without credentials/query/fragment")
        if not _loopback(self.bind) and not self.allow_remote_bind:
            raise ValueError("Non-loopback binding requires RELAY_ALLOW_REMOTE_BIND=true")
        if (len(self.credential) < 32 and not self.journal_directory) or len(self.local_token) < 32:
            raise ValueError("RELAY_CREDENTIAL and RELAY_LOCAL_TOKEN must each contain at least 32 characters")
        if self.credential == self.local_token:
            raise ValueError("Local token must be distinct from the central credential")
        if self.journal_directory and os.name != "posix":
            raise ValueError("Durable relay rotation currently requires a POSIX filesystem")
        if not 600 <= self.rotation_interval_seconds <= 2592000:
            raise ValueError("Rotation interval must be between 600 and 2592000 seconds")
        if not 1 <= self.port <= 65535:
            raise ValueError("RELAY_PORT is invalid")
        if not 1 <= self.max_body_bytes <= 32 * 1024 * 1024:
            raise ValueError("Relay request body limit is invalid")
        if not math.isfinite(self.request_timeout_seconds) or self.request_timeout_seconds <= 0:
            raise ValueError("Relay timeout must be finite and positive")

    @classmethod
    def from_env(cls) -> "RelaySettings":
        return cls(
            central_url=os.environ.get("CENTRAL_GATEWAY_URL", "http://127.0.0.1:8080"),
            credential=os.environ.get("RELAY_CREDENTIAL", ""),
            local_token=os.environ.get("RELAY_LOCAL_TOKEN", ""),
            bind=os.environ.get("RELAY_BIND", "127.0.0.1"),
            port=int(os.environ.get("RELAY_PORT", "8090")),
            journal_directory=os.environ.get("RELAY_JOURNAL_DIRECTORY"),
            rotation_interval_seconds=int(os.environ.get("RELAY_ROTATION_INTERVAL_SECONDS", "86400")),
            allow_remote_bind=os.environ.get("RELAY_ALLOW_REMOTE_BIND", "false").lower() == "true",
        )


class TokenBroker:
    """In-memory token cache; one exchange at a time, even under concurrency."""

    def __init__(self, settings: RelaySettings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._token: str | None = None
        self._refresh_at = 0.0
        self._lock = asyncio.Lock()
        self._journal = None

    async def token(self, rejected_token: str | None = None) -> str:
        async with self._lock:
            # Another request may already have refreshed the rejected token.
            if rejected_token is not None and self._token == rejected_token:
                self._token = None
                self._refresh_at = 0.0
            if self._token is not None and time.monotonic() < self._refresh_at:
                return self._token
            self._token = None  # Never fall back to stale tokens after an exchange failure.
            try:
                credential = self.settings.credential
                if self.settings.journal_directory:
                    from .relay_journal import CredentialJournal, RotationError
                    try:
                        if self._journal is None:
                            self._journal = CredentialJournal(self.settings.journal_directory,
                                                              self.settings.central_url, credential)
                        credential = await self._journal.credential(self.client, self.settings.rotation_interval_seconds)
                    except (RotationError, OSError, ValueError, KeyError, TypeError):
                        raise HTTPException(503, "Durable agent rotation unavailable; inspect private journal") from None
                if credential == self.settings.local_token:
                    raise HTTPException(503, "Local token must differ from durable root credential")
                response = await self.client.post(
                    self.settings.central_url.rstrip("/") + "/auth/agent/exchange",
                    headers={"Authorization": "Bearer " + credential},
                    follow_redirects=False,
                )
                if response.status_code != 200:
                    raise HTTPException(503, "Gateway credential exchange failed")
                payload = response.json()
                token = payload["access_token"]
                ttl = float(payload["expires_in"])
                if (
                    not isinstance(token, str)
                    or not token
                    or len(token) > 16384
                    or not math.isfinite(ttl)
                    or not 0 < ttl <= 3600
                    or payload.get("token_type", "Bearer").lower() != "bearer"
                ):
                    raise ValueError("Invalid token response")
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(503, "Gateway credential exchange unavailable") from None
            self._token = token
            self._refresh_at = time.monotonic() + ttl - min(30.0, ttl / 5)
            return token


def create_relay_app(settings: RelaySettings | None = None, client: httpx.AsyncClient | None = None) -> FastAPI:
    """Application factory also allows a mock HTTP transport for offline tests."""
    settings = settings or RelaySettings.from_env()
    owned = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=False,
        trust_env=False,
    )
    broker = TokenBroker(settings, client)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if broker._journal is not None:
                broker._journal.close()
            if owned:
                await client.aclose()

    app = FastAPI(title="Local LLM Relay", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.broker = broker

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    async def forward(request: Request):
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + settings.local_token
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(401, "Invalid local relay token")
        # Do not proxy arbitrary URLs or routes; the caller only reaches these allowlisted paths.
        if request.url.query:
            raise HTTPException(400, "Query parameters are not supported")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > settings.max_body_bytes:
                raise HTTPException(413, "Request body too large")
            body.extend(chunk)
        if request.method == "POST" and request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
            raise HTTPException(415, "Expected application/json")
        token = await broker.token()
        upstream: httpx.Response | None = None
        try:
            for attempt in range(2):
                outbound = client.build_request(
                    request.method,
                    settings.central_url.rstrip("/") + request.url.path,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                    content=bytes(body),
                )
                upstream = await client.send(outbound, stream=True, follow_redirects=False)
                # Only retry a rejected authentication response before returning any bytes.
                if upstream.status_code == 401 and attempt == 0:
                    await upstream.aclose()
                    token = await broker.token(rejected_token=token)
                    continue
                break
        except httpx.HTTPError:
            if upstream is not None:
                await upstream.aclose()
            return JSONResponse({"detail": "Central gateway unavailable"}, status_code=502)
        assert upstream is not None
        if upstream.status_code == 401:
            await upstream.aclose()
            raise HTTPException(503, "Central gateway rejected relay credentials")
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            raise HTTPException(502, "Central gateway redirect rejected")

        async def response_body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        # Never leak cookies, credentials, hop-by-hop or provider-specific auth headers.
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        content_type = upstream.headers.get("content-type", "application/json")
        headers["Content-Type"] = content_type
        for name in ("x-request-id", "retry-after"):
            if name in upstream.headers:
                headers[name] = upstream.headers[name]
        return StreamingResponse(response_body(), status_code=upstream.status_code, headers=headers)

    app.add_api_route("/v1/chat/completions", forward, methods=["POST"])
    app.add_api_route("/v1/responses", forward, methods=["POST"])
    app.add_api_route("/v1/embeddings", forward, methods=["POST"])
    app.add_api_route("/v1/models", forward, methods=["GET"])
    return app


def main() -> None:
    import uvicorn
    settings = RelaySettings.from_env()
    uvicorn.run(create_relay_app(settings), host=settings.bind, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
