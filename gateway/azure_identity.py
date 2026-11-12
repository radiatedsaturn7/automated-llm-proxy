"""Deployment-owned, opt-in Azure OpenAI workload identity (public Azure only)."""
import asyncio
import json
import math
import re
import time
from urllib.parse import urlparse

from fastapi import HTTPException

SCOPE = "https://cognitiveservices.azure.com/.default"
KIND = "azure_openai_workload_identity"


def valid_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlparse(value)
    return (parsed.scheme == "https" and parsed.netloc == parsed.hostname
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.openai\.azure\.com", parsed.hostname or "")
            and parsed.path == "/openai/v1" and not parsed.query and not parsed.fragment
            and value == f"https://{parsed.hostname}/openai/v1")


def validate_endpoint_list(raw, enabled):
    try:
        values = json.loads(raw)
        if (not isinstance(values, list) or len(values) > 100 or any(not valid_endpoint(x) for x in values)
                or len(values) != len(set(values)) or (enabled and not values)):
            raise ValueError()
        return values
    except (ValueError, TypeError):
        raise ValueError("Azure workload identity requires exact public Azure OpenAI v1 HTTPS endpoints") from None


def check_endpoint(endpoint):
    from .config import get_settings
    cfg = get_settings()
    allowed = validate_endpoint_list(cfg.azure_workload_identity_allowed_endpoints,
                                     cfg.azure_workload_identity_enabled)
    if not cfg.azure_workload_identity_enabled or endpoint not in allowed:
        raise HTTPException(400, "Azure workload identity endpoint is not enabled by the deployment")


def create_credential(cfg):
    if not cfg.azure_workload_identity_enabled:
        return None
    from azure.core.pipeline.transport import AioHttpTransport
    from azure.identity.aio import WorkloadIdentityCredential
    try:
        # Only official deployment environment (AZURE_TENANT_ID, CLIENT_ID,
        # FEDERATED_TOKEN_FILE), never stored provider configuration. No CLI,
        # shared cache, client-secret or metadata credential fallback.
        return WorkloadIdentityCredential(authority="https://login.microsoftonline.com",
            transport=AioHttpTransport(use_env_settings=False), logging_enable=False,
            retry_total=0, connection_timeout=cfg.azure_identity_timeout_seconds,
            read_timeout=cfg.azure_identity_timeout_seconds)
    except Exception:
        raise RuntimeError("Azure workload identity deployment configuration is invalid") from None


async def authorization(app, endpoint):
    check_endpoint(endpoint)  # Revalidate at dispatch, not only registration.
    from .config import get_settings
    credential = getattr(app.state, "azure_workload_credential", None)
    if credential is None:
        raise HTTPException(503, "Azure workload identity is unavailable")
    try:
        async with asyncio.timeout(get_settings().azure_identity_timeout_seconds):
            token = await credential.get_token(SCOPE)
        if (not isinstance(token.token, str) or not token.token or len(token.token) > 32768
                or any(ord(c) < 33 or ord(c) > 126 for c in token.token)
                or type(token.expires_on) not in {int, float}
                or not math.isfinite(token.expires_on) or token.expires_on <= time.time() + 5):
            raise ValueError()
        return "Bearer " + token.token
    except Exception:
        # Never reflect SDK errors/assertions/tokens in response or logs.
        raise HTTPException(503, "Azure workload identity authentication failed") from None


async def close_credential(app):
    credential = getattr(app.state, "azure_workload_credential", None)
    app.state.azure_workload_credential = None
    if credential is not None:
        try:
            async with asyncio.timeout(5):
                await credential.close()
        except Exception:  # nosec B110 # Credential teardown must not prevent HTTP/DB shutdown; never log SDK secrets.
            pass  # A credential cleanup failure must not skip HTTP/DB shutdown.
