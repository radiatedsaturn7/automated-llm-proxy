"""Bounded, stable-window administrative exports for customer-controlled sinks."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from . import db
from .admin import record
from .config import get_settings
from .security import Identity, admin, aware

router = APIRouter(prefix="/admin/api/exports", tags=["exports"])


def encode(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(
        get_settings().gateway_signing_key.encode(), b"export-v1:" + raw, hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(raw + signature).decode()


def decode(token):
    try:
        if len(token) > 4096:
            raise ValueError()
        data = base64.b64decode(token, altchars=b"-_", validate=True)
        raw, signature = data[:-32], data[-32:]
        expected = hmac.new(
            get_settings().gateway_signing_key.encode(), b"export-v1:" + raw, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "kind",
            "actor",
            "since",
            "until",
            "after_time",
            "after_id",
        }:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, "Invalid export cursor") from None


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(400, "Timestamp must be timezone-aware ISO8601") from None


@router.get("/{kind}")
async def export(
    kind: str,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    identity: Identity = Depends(admin),
):
    if kind not in {"audit", "usage"}:
        raise HTTPException(404, "Unknown export type")
    model = db.Audit if kind == "audit" else db.Usage
    position = None
    if cursor:
        if since or until:
            raise HTTPException(400, "Cursor already fixes the export window")
        position = decode(cursor)
        if position["kind"] != kind or position["actor"] != identity.user.id:
            raise HTTPException(400, "Cursor belongs to another export")
        start, end = timestamp(position["since"]), timestamp(position["until"])
    else:
        end = timestamp(until) if until else db.now()
        start = timestamp(since) if since else end - timedelta(days=1)
    if start >= end or end - start > timedelta(days=31) or end > db.now():
        raise HTTPException(400, "Use a past window of at most 31 days")
    query = select(model).where(model.created_at >= start, model.created_at < end)
    if position:
        after = timestamp(position["after_time"])
        query = query.where(
            or_(model.created_at > after, and_(model.created_at == after, model.id > position["after_id"]))
        )
    async with db.session_factory() as session:
        values = list(
            (await session.scalars(query.order_by(model.created_at, model.id).limit(limit + 1))).all()
        )
    more = len(values) > limit
    values = values[:limit]
    next_cursor = None
    if more:
        last = values[-1]
        next_cursor = encode(
            {
                "kind": kind,
                "actor": identity.user.id,
                "since": start.isoformat(),
                "until": end.isoformat(),
                "after_time": aware(last.created_at).isoformat(),
                "after_id": last.id,
            }
        )
    return {
        "kind": kind,
        "since": start.isoformat(),
        "until": end.isoformat(),
        "records": [record(row) for row in values],
        "next_cursor": next_cursor,
    }
