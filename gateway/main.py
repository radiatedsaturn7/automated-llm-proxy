import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import local_mfa  # noqa: F401
from . import local_auth
from . import account, admin, agent_rotation, auth, comparisons, db, enrollment, exports, inference, prompts, responses, scim, teams
from .config import get_settings
from .observability import ObservabilityMiddleware
from .migrate import HEAD, versions


@asynccontextmanager
async def lifespan(app):
    cfg = get_settings()
    if db.engine is None:
        db.init_engine()
    app.state.upstream = httpx.AsyncClient(timeout=httpx.Timeout(cfg.provider_timeout_seconds, connect=10),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50), follow_redirects=False, trust_env=False)
    app.state.accounting_tasks = set()
    yield
    # Finishing a watchdog can spawn a settlement task. Drain every generation
    # before disposing the upstream client/database, not just the first snapshot.
    while app.state.accounting_tasks:
        snapshot = list(app.state.accounting_tasks)
        await asyncio.gather(*snapshot, return_exceptions=True)
        app.state.accounting_tasks.difference_update(snapshot)
    await app.state.upstream.aclose()
    await db.engine.dispose()


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = get_settings().max_request_bytes
        # Read a bounded body before dispatch, so chunked uploads cannot bypass Content-Length.
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                return await response(scope, receive, send)
            more = message.get("more_body", False)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


app = FastAPI(title="Thunderbolt", version="0.1.0", lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware)
app.include_router(teams.router)
app.include_router(comparisons.router)
app.include_router(exports.router)
app.include_router(prompts.router)
app.include_router(account.router)
app.include_router(scim.router)
app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(local_auth.router)
app.include_router(agent_rotation.router)
app.include_router(enrollment.router)
app.include_router(inference.router)
app.include_router(responses.router)
app.mount("/admin/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers["Cache-Control"] = "no-store"
    if get_settings().environment == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Pydantic's default errors include raw input and validator context. Those
    # can contain API credentials or evaluation prompts; never echo them.
    return JSONResponse({"detail": [{"loc": list(error["loc"]), "type": error["type"]}
                                    for error in exc.errors()]}, status_code=422)


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    return JSONResponse({"detail": "Record conflicts with an existing record"}, status_code=409)


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    logging.getLogger("gateway").error("database operation failed", extra={"error_type": type(exc).__name__})
    return JSONResponse({"detail": "Policy database unavailable"}, status_code=503)


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    try:
        async with db.session_factory() as s:
            row = await s.scalar(select(db.PolicyLock.id).where(db.PolicyLock.id == 1))
            applied = (await s.execute(select(versions.c.version).order_by(versions.c.version)
                                       .limit(HEAD + 1))).scalars().all()
            if row != 1 or applied != list(range(1, HEAD + 1)):
                raise RuntimeError("Schema requires migration")
    except Exception:
        raise HTTPException(503, "Database/schema unavailable") from None
    return {"status": "ready"}


@app.get("/metrics")
async def metrics(request: Request):
    expected = get_settings().metrics_token
    if not expected or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected):
        raise HTTPException(401, "Metrics credential required")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# Registered last so body limits, auth errors and security-header middleware are observed.
app.add_middleware(ObservabilityMiddleware)


@app.get("/portal", include_in_schema=False)
async def user_portal():
    return FileResponse(Path(__file__).parent / "static" / "portal.html")


@app.get("/team-admin", include_in_schema=False)
async def team_admin_page():
    return FileResponse(Path(__file__).parent / "static" / "teams.html")


@app.get("/relay-enroll", include_in_schema=False)
async def relay_enrollment_page():
    return FileResponse(Path(__file__).parent / "static" / "enroll.html")
