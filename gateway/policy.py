import json
from decimal import Decimal, ROUND_CEILING

from fastapi import HTTPException
from sqlalchemy import func, select

from . import db
from .security import aware


def price(input_tokens, output_tokens, route):
    # $/million tokens numerically equals microdollars/token; no float accumulation.
    return int((Decimal(str(route["input_cost_per_million"])) * input_tokens +
                Decimal(str(route["output_cost_per_million"])) * output_tokens).to_integral_value(rounding=ROUND_CEILING))


def token_bound(payload):
    # Conservative text-byte heuristic, not a tokenizer or provider billing guarantee.
    clean = {k: v for k, v in payload.items() if k not in {"max_tokens", "max_completion_tokens", "stream"}}
    return len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) + 256


async def reserve(identity, amount, tokens, model):
    async with db.transaction() as s:
        key = await s.get(db.APIKey, identity.key.id)
        user = await s.get(db.User, identity.user.id)
        if not key or key.revoked or aware(key.expires_at) <= db.now() or not user or not user.enabled:
            raise HTTPException(401, "Credential changed")
        if key.team_id != user.team_id:
            raise HTTPException(403, "Credential team changed")
        if key.allowed_models and model not in key.allowed_models:
            raise HTTPException(403, "Model not allowed by key")
        team = await s.get(db.Team, key.team_id) if key.team_id else None
        if key.team_id and (not team or not team.enabled):
            raise HTTPException(403, "Team disabled")
        period = db.now().strftime("%Y-%m")
        for entity, field in [(user, "user_id"), (team, "team_id")]:
            if entity is None or entity.budget_micros is None:
                continue
            spent = await s.scalar(select(func.coalesce(func.sum(db.Usage.cost_micros), 0)).where(
                getattr(db.Usage, field) == entity.id, db.Usage.period == period))
            pending = await s.scalar(select(func.coalesce(func.sum(db.Reservation.amount_micros), 0)).where(
                getattr(db.Reservation, field) == entity.id, db.Reservation.period == period,
                db.Reservation.status == "pending"))
            if spent + pending + amount > entity.budget_micros:
                raise HTTPException(429, f"{field[:-3].title()} monthly budget exceeded")
        bucket_id = key.id + ":" + db.now().strftime("%Y%m%d%H%M")
        bucket = await s.get(db.RateBucket, bucket_id)
        if bucket is None:
            bucket = db.RateBucket(id=bucket_id, requests=0, tokens=0)
            s.add(bucket)
        if bucket.requests + 1 > key.rpm or bucket.tokens + tokens > key.tpm:
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})
        bucket.requests += 1
        bucket.tokens += tokens
        reservation = db.Reservation(user_id=user.id, team_id=key.team_id, key_id=key.id,
                                     period=period, amount_micros=amount)
        s.add(reservation)
        await s.flush()
        await db.audit(s, user.id, "inference.reserve", reservation.id, model=model)
        return reservation.id


async def finalize(reservation_id, model, provider_id, route, usage=None, release=False):
    async with db.transaction() as s:
        reservation = await s.get(db.Reservation, reservation_id)
        if not reservation or reservation.status != "pending":
            return
        if release:
            reservation.status = "released"
            await db.audit(s, reservation.user_id, "inference.rejected", reservation.id)
            return
        inp, out = 0, 0
        estimated = True
        if isinstance(usage, dict):
            inp = usage.get("prompt_tokens", usage.get("input_tokens"))
            out = usage.get("completion_tokens", usage.get("output_tokens", 0))
            if isinstance(inp, int) and not isinstance(inp, bool) and isinstance(out, int) and not isinstance(out, bool) and 0 <= inp <= 2_147_483_647 and 0 <= out <= 2_147_483_647:
                estimated = False
            else:
                inp, out = 0, 0
        cost = reservation.amount_micros if estimated else price(inp, out, route)
        s.add(db.Usage(reservation_id=reservation.id, user_id=reservation.user_id, team_id=reservation.team_id,
                       key_id=reservation.key_id, period=reservation.period, model=model, provider_id=provider_id,
                       input_tokens=inp, output_tokens=out, cost_micros=cost, estimated=estimated))
        reservation.status = "settled"
        await db.audit(s, reservation.user_id, "inference.complete", reservation.id,
                       model=model, estimated=estimated, over_reservation=cost > reservation.amount_micros)
