"""Shared next-request routing. Never retry a request after provider dispatch."""
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import db
from .config import get_settings
from .security import aware


@dataclass(frozen=True)
class Ticket:
    provider_id: str
    model_alias: str
    epoch: int
    probe_id: str | None


async def clock(session):
    # Use the shared database clock, not the clock of the admitting replica.
    return await db.database_now(session)


async def locked_health(session, provider_id, model, now):
    insert = pg_insert if db.engine.dialect.name == 'postgresql' else sqlite_insert
    await session.execute(insert(db.ProviderHealth).values(
        provider_id=provider_id, model_alias=model, state='closed', failures=0,
        epoch=0, updated_at=now).on_conflict_do_nothing(index_elements=['provider_id', 'model_alias']))
    return await session.scalar(select(db.ProviderHealth).where(
        db.ProviderHealth.provider_id == provider_id, db.ProviderHealth.model_alias == model).with_for_update())


async def choose(model, endpoint):
    cfg = get_settings()
    async with db.transaction(inference=True) as session:
        providers = list((await session.scalars(select(db.Provider).where(db.Provider.enabled.is_(True))
                            .order_by(db.Provider.priority, db.Provider.id))).all())
        candidates = [p for p in providers if model in p.models and
                      (endpoint != 'embeddings' or p.kind in {'openai', 'azure_openai', 'azure_openai_workload_identity', 'gemini'})]
        if not candidates:
            raise HTTPException(404, 'Configured model not available')
        if not cfg.circuit_breaker_enabled:
            return candidates[0], None
        now = await clock(session)
        waits = []
        for provider in candidates:
            row = await locked_health(session, provider.id, model, now)
            if row.state == 'closed':
                return provider, Ticket(provider.id, model, row.epoch, None)
            deadline = row.probe_until if row.state == 'probing' else row.open_until
            if deadline and aware(deadline) > now:
                waits.append(max(1, int((aware(deadline) - now).total_seconds()) + 1))
                continue
            # The row lock elects one probe across all application replicas.
            row.epoch += 1
            row.state, row.probe_id = 'probing', db.uid()
            row.probe_until = now + timedelta(seconds=cfg.effective_circuit_probe_seconds)
            row.updated_at = now
            return provider, Ticket(provider.id, model, row.epoch, row.probe_id)
        raise HTTPException(503, 'All configured routes are temporarily unavailable',
                            headers={'Retry-After': str(min(waits) if waits else cfg.circuit_cooldown_seconds)})


async def record(ticket, outcome, retry_after=None):
    if ticket is None:
        return
    cfg = get_settings()
    async with db.transaction(inference=True) as session:
        row = await session.scalar(select(db.ProviderHealth).where(
            db.ProviderHealth.provider_id == ticket.provider_id,
            db.ProviderHealth.model_alias == ticket.model_alias).with_for_update())
        if row is None or row.epoch != ticket.epoch or row.probe_id != ticket.probe_id:
            return  # Late completion from an obsolete circuit/configuration generation.
        now = await clock(session)
        row.last_outcome, row.updated_at = outcome, now
        if outcome == 'success':
            row.state, row.failures = 'closed', 0
            row.open_until = row.probe_id = row.probe_until = None
            return
        if outcome in {'failure', 'rate_limited', 'configuration_error'}:
            row.failures = min(row.failures + 1, 1_000_000)
        must_open = (row.state == 'probing' or row.failures >= cfg.circuit_failure_threshold
                     or outcome in {'rate_limited', 'configuration_error'})
        if must_open:
            cooldown = cfg.circuit_cooldown_seconds
            if outcome == 'rate_limited' and retry_after is not None:
                try:
                    cooldown = min(3600, max(cooldown, int(retry_after)))
                except (TypeError, ValueError):
                    pass  # Do not trust arbitrary upstream header values.
            row.state = 'open'
            row.epoch += 1
            row.open_until = now + timedelta(seconds=cooldown)
            row.probe_id = row.probe_until = None


async def abandon(ticket):
    """Release a probe only when admission/validation failed BEFORE dispatch."""
    if ticket is None or ticket.probe_id is None:
        return
    async with db.transaction(inference=True) as session:
        row = await session.scalar(select(db.ProviderHealth).where(
            db.ProviderHealth.provider_id == ticket.provider_id,
            db.ProviderHealth.model_alias == ticket.model_alias).with_for_update())
        if row and row.epoch == ticket.epoch and row.probe_id == ticket.probe_id:
            row.state, row.open_until = 'open', await clock(session)
            row.epoch += 1
            row.probe_id = row.probe_until = None
            row.updated_at = row.open_until


async def reset(session, provider_id):
    """Called under exclusive policy gate on operator configuration changes."""
    rows = list((await session.scalars(select(db.ProviderHealth).where(
        db.ProviderHealth.provider_id == provider_id).with_for_update())).all())
    now = await clock(session)
    for row in rows:
        row.epoch += 1
        row.state, row.failures, row.last_outcome = 'closed', 0, 'operator_reset'
        row.open_until = row.probe_id = row.probe_until = None
        row.updated_at = now


def classify(status):
    if status == 429:
        return 'rate_limited'
    if status in {401, 403}:
        return 'configuration_error'
    return 'failure' if status >= 500 or 300 <= status < 400 else 'rejected'
