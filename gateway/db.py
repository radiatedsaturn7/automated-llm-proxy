import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class PolicyLock(Base):
    __tablename__ = "policy_lock"
    id: Mapped[int] = mapped_column(primary_key=True)


class Team(Base):
    __tablename__ = "teams"
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    budget_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"
    scim_managed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    budget_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(512), unique=True, nullable=True)


class TeamMembership(Base):
    __tablename__ = "team_memberships"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="member")


async def active_membership(session, user, team_id):
    if not user or not user.enabled:
        return False
    if team_id is None:
        return user.team_id is None
    team = await session.get(Team, team_id)
    if not team or not team.enabled:
        return False
    return user.team_id == team_id or await session.get(TeamMembership, (user.id, team_id)) is not None


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(32), default="openai")
    base_url: Mapped[str] = mapped_column(String(2048))
    encrypted_key: Mapped[str] = mapped_column(Text)
    models: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)


class ProviderHealth(Base):
    __tablename__ = "provider_health"
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), primary_key=True)
    model_alias: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="closed")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    probe_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    probe_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class APIKey(Base):
    __tablename__ = "api_keys"
    enrollment_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True, index=True)
    __table_args__ = (Index("ix_api_keys_user_created_id", "user_id", "created_at", "id"),)
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_rotation_operation: Mapped[str | None] = mapped_column(String(36), nullable=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="api")
    permissions: Mapped[list] = mapped_column(JSON, default=lambda: ["inference"], server_default=text('\'["inference"]\''))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_models: Mapped[list] = mapped_column(JSON, default=list)
    rpm: Mapped[int] = mapped_column(Integer, default=60)
    tpm: Mapped[int] = mapped_column(Integer, default=100000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentEnrollment(Base):
    __tablename__ = "agent_enrollments"
    approval_code: Mapped[str] = mapped_column(String(36), unique=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_digest: Mapped[str] = mapped_column(String(64), unique=True)
    device_name: Mapped[str] = mapped_column(String(120))
    policy: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_poll: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"), nullable=True)


class AgentRotation(Base):
    __tablename__ = "agent_rotations"
    key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36))
    pending_digest: Mapped[str] = mapped_column(String(64), unique=True)
    generation: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (Index("ix_reservations_user_period_status", "user_id", "period", "status"),
                      Index("ix_reservations_team_period_status", "team_id", "period", "status"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36))
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    key_id: Mapped[str] = mapped_column(String(36))
    period: Mapped[str] = mapped_column(String(7), index=True)
    amount_micros: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Pending reservations deliberately remain charged after crash until explicit reconciliation.


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = (Index("ix_usage_user_period", "user_id", "period"),
                      Index("ix_usage_team_period", "team_id", "period"),
                      Index("ix_usage_created_id", "created_at", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    reservation_id: Mapped[str] = mapped_column(String(36), unique=True)
    user_id: Mapped[str] = mapped_column(String(36))
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    key_id: Mapped[str] = mapped_column(String(36))
    model: Mapped[str] = mapped_column(String(128))
    provider_id: Mapped[str] = mapped_column(String(36))
    period: Mapped[str] = mapped_column(String(7), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_micros: Mapped[int] = mapped_column(BigInteger)
    estimated: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class Audit(Base):
    __tablename__ = "audit"
    __table_args__ = (Index("ix_audit_created_id", "created_at", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OIDCState(Base):
    __tablename__ = "oidc_states"
    __table_args__ = (Index("ix_oidc_states_expiry_digest", "expires_at", "digest"),)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(128))
    verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    __table_args__ = (Index("ix_browser_sessions_expiry_digest", "expires_at", "digest"),
                      Index("ix_browser_sessions_user", "user_id"),
                      Index("ix_browser_sessions_oidc_sid", "oidc_issuer", "oidc_sid"),
                      Index("ix_browser_sessions_oidc_subject", "oidc_issuer", "oidc_subject"))
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    csrf: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    oidc_issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oidc_sid: Mapped[str | None] = mapped_column(String(512), nullable=True)


class OIDCLogoutReplay(Base):
    __tablename__ = "oidc_logout_replays"
    __table_args__ = (Index("ix_oidc_logout_replays_issuer", "issuer"),)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sid: Mapped[str | None] = mapped_column(String(512), nullable=True)
    issued_at: Mapped[int] = mapped_column(Integer)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("template_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    template_id: Mapped[str] = mapped_column(ForeignKey("prompt_templates.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    variables: Mapped[list] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    prompt_version_id: Mapped[str] = mapped_column(ForeignKey("prompt_versions.id"), index=True)
    rules: Mapped[list] = mapped_column(JSON)
    report: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


engine = None
session_factory = None
sqlite_lock = asyncio.Lock()


def init_engine(url=None):
    global engine, session_factory
    url = url or get_settings().database_url
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    else:
        settings = get_settings()
        kwargs.update(
            pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds, pool_recycle=settings.db_pool_recycle_seconds,
            isolation_level="READ COMMITTED",
            connect_args={"server_settings": {
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
            }},
        )
    engine = create_async_engine(url, **kwargs)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine


@asynccontextmanager
async def transaction(*, inference=False):
    """Policy writer gate; inference shares the gate and then locks its subjects.

    Always acquire this gate before any subject row lock. Existing control-plane
    callers retain the exclusive gate, so revocations and budget/identity edits
    cannot interleave with admission/accounting decisions. Never upgrade a shared
    gate to exclusive inside a transaction. SQLite remains serialized/dev-only.
    """
    if session_factory is None:
        init_engine()
    async with session_factory() as session:
        async with session.begin():
            if engine.url.drivername.startswith("sqlite"):
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                locked = await session.scalar(select(PolicyLock.id).where(PolicyLock.id == 1).with_for_update(read=inference))
                if locked != 1:
                    raise SQLAlchemyError("Policy lock missing; run schema migration")
            yield session


async def audit(session, actor, action, target=None, **details):
    session.add(Audit(actor_id=actor, action=action, target_id=target, details=details))


async def lock_policy_subjects(session, team_id, user_id, key_id):
    """Lock in universal Team -> User -> APIKey order, under the shared gate.

    Subject identifiers must be read *after* taking the gate. The exclusive gate
    used by all control-plane writes keeps ownership stable during these locks.
    At READ COMMITTED, later budget reads see commits from previous row holders.
    """
    rows = []
    for model, identifier in ((Team, team_id), (User, user_id), (APIKey, key_id)):
        row = None
        if identifier is not None:
            row = await session.scalar(select(model).where(model.id == identifier)
                                       .with_for_update().execution_options(populate_existing=True))
        rows.append(row)
    return tuple(rows)


class LocalPassword(Base):
    __tablename__ = "local_passwords"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(256))


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    bucket: Mapped[str] = mapped_column(String(32), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
