import json
from dataclasses import dataclass
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


@dataclass(frozen=True, repr=False)
class ProviderSnapshot:
    provider_id: str
    enabled: bool
    kind: str
    base_url: str
    encrypted_key: str
    priority: int
    route_json: str


def provider_snapshot(provider, model):
    # Capture no plaintext secret. Suppress repr so ciphertext/configuration
    # cannot accidentally enter diagnostics through a dataclass representation.
    return ProviderSnapshot(provider.id, provider.enabled, provider.kind, provider.base_url,
                            provider.encrypted_key, provider.priority,
                            json.dumps(provider.models.get(model), sort_keys=True, separators=(",", ":")))


async def reserve(identity, amount, tokens, model, *, selected_provider=None):
    async with db.transaction(inference=True) as s:
        # Admin configuration writes hold the exclusive policy gate. Compare
        # inside admission's shared gate before any accounting/rate mutation.
        if selected_provider is not None:
            provider = await s.get(db.Provider, selected_provider.provider_id)
            if (not provider or not provider.enabled or model not in provider.models
                    or provider_snapshot(provider, model) != selected_provider):
                raise HTTPException(409, "Selected provider configuration changed before admission")
        key = await s.get(db.APIKey, identity.key.id)
        if key is None:
            raise HTTPException(401, "Credential changed")
        team, user, key = await db.lock_policy_subjects(s, key.team_id, key.user_id, key.id)
        # Sample after contention: transaction-start time can predate a lock wait.
        admitted_at = await db.database_now(s)
        if not key or key.revoked or aware(key.expires_at) <= admitted_at or not user or not user.enabled:
            raise HTTPException(401, "Credential changed")
        if key.kind != "agent" and "inference" not in key.permissions:
            raise HTTPException(403, "Inference permission required")
        if key.kind == "agent" and identity.generation != key.generation:
            raise HTTPException(401, "Agent credential generation changed")
        if key.user_id != identity.user.id or not await db.active_membership(s, user, key.team_id):
            raise HTTPException(403, "Credential team changed")
        if key.allowed_models and model not in key.allowed_models:
            raise HTTPException(403, "Model not allowed by key")
        if key.team_id and (not team or not team.enabled or team.deleted_at is not None):
            raise HTTPException(403, "Team disabled")
        period = admitted_at.strftime("%Y-%m")
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
        family = "member:" + user.id if key.self_service else key.id
        bucket_id = family + ":" + admitted_at.strftime("%Y%m%d%H%M")
        bucket = await s.get(db.RateBucket, bucket_id)
        if bucket is None:
            bucket = db.RateBucket(id=bucket_id, requests=0, tokens=0)
            s.add(bucket)
        if bucket.requests + 1 > key.rpm or bucket.tokens + tokens > key.tpm:
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})
        from .allowances import consume
        await consume(s, user.id, admitted_at)
        bucket.requests += 1
        bucket.tokens += tokens
        reservation = db.Reservation(user_id=user.id, team_id=key.team_id, key_id=key.id,
                                     period=period, amount_micros=amount)
        s.add(reservation)
        await s.flush()
        await db.audit(s, user.id, "inference.reserve", reservation.id, model=model)
        return reservation.id


async def inherit_rotation_allowance(session, previous, replacement):
    """Carry consumed allowance under the caller's exclusive policy writer gate.

    Both key IDs must already exist in this transaction. Admission holds the
    shared gate, so no old-key admission can race this copy and revocation.
    Copy only the current shared-DB minute: prior windows must not penalize the
    replacement, and a boundary crossed after this sample starts a fresh window.
    """
    if previous.self_service:
        from .member_keys import inherit_member_origin
        await inherit_member_origin(session, previous, replacement)
        return
    minute = (await db.database_now(session)).strftime("%Y%m%d%H%M")
    bucket = await session.get(db.RateBucket, previous.id + ":" + minute)
    if bucket is not None:
        session.add(db.RateBucket(id=replacement.id + ":" + minute,
                                 requests=bucket.requests, tokens=bucket.tokens))


async def finalize(reservation_id, model, provider_id, route, usage=None, release=False):
    async with db.transaction(inference=True) as s:
        reservation = await s.get(db.Reservation, reservation_id)
        if not reservation:
            return
        await db.lock_policy_subjects(s, reservation.team_id, reservation.user_id, reservation.key_id)
        reservation = await s.scalar(select(db.Reservation).where(db.Reservation.id == reservation_id)
                                     .with_for_update().execution_options(populate_existing=True))
        if reservation.status != "pending":
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
