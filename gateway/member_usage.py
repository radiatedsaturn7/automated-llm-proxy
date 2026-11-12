"""Own settled usage history with bounded windows and actor-bound keyset pagination."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, tuple_

from . import db
from .account import browser_member
from .config import get_settings
from .security import Identity, aware, revalidate_identity

router = APIRouter(prefix="/account/api")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(400, "Timestamp must be timezone-aware ISO8601") from None


def encode_cursor(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(get_settings().gateway_signing_key.encode(),
                         b"account-usage-v1:" + raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode()


def decode_cursor(token, actor):
    try:
        if len(token) > 2048:
            raise ValueError()
        data = base64.b64decode(token, altchars=b"-_", validate=True)
        raw, signature = data[:-32], data[-32:]
        expected = hmac.new(get_settings().gateway_signing_key.encode(),
                            b"account-usage-v1:" + raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"actor", "since", "until", "before_time", "before_id"}
                or value["actor"] != actor or not all(isinstance(v, str) for v in value.values())):
            raise ValueError()
        UUID(value["before_id"])
        return value
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, "Invalid usage cursor") from None


@router.get("/usage")
async def usage_history(
    since: str | None = Query(None, max_length=80),
    until: str | None = Query(None, max_length=80),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(50, ge=1, le=100),
    identity: Identity = Depends(browser_member),
):
    position = None
    if cursor:
        if since is not None or until is not None:
            raise HTTPException(400, "Cursor already fixes the usage window")
        position = decode_cursor(cursor, identity.user.id)
        start, end = timestamp(position["since"]), timestamp(position["until"])
    else:
        start, end = timestamp(since) if since else None, timestamp(until) if until else None
    async with db.transaction(inference=True) as session:
        current = await revalidate_identity(session, identity)
        now = await db.database_now(session)
        end = end or now
        try:
            start = start or end - timedelta(days=30)
        except OverflowError:
            raise HTTPException(400, "Usage window is out of range") from None
        if start >= end or end - start > timedelta(days=31) or end > now:
            raise HTTPException(400, "Use a past usage window of at most 31 days")
        predicate = (db.Usage.user_id == current.user.id, db.Usage.created_at >= start, db.Usage.created_at < end)
        query = select(db.Usage).where(*predicate)
        if position:
            before = timestamp(position["before_time"])
            if not start <= before < end:
                raise HTTPException(400, "Invalid usage cursor position")
            query = query.where(tuple_(db.Usage.created_at, db.Usage.id) < tuple_(before, position["before_id"]))
        rows = list((await session.scalars(query.order_by(db.Usage.created_at.desc(), db.Usage.id.desc())
                                          .limit(limit + 1))).all())
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor({"actor": current.user.id, "since": start.isoformat(), "until": end.isoformat(),
                                         "before_time": aware(last.created_at).isoformat(), "before_id": last.id})
        records = [{"id": row.id, "created_at": aware(row.created_at).isoformat(), "model": row.model,
                    "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
                    "cost_micros": row.cost_micros, "estimated": row.estimated} for row in rows[:limit]]
    return {"since": start.isoformat(), "until": end.isoformat(), "records": records, "next_cursor": next_cursor}
