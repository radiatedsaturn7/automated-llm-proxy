import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, select, text
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
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    budget_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(512), unique=True, nullable=True)


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


class APIKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="api")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_models: Mapped[list] = mapped_column(JSON, default=list)
    rpm: Mapped[int] = mapped_column(Integer, default=60)
    tpm: Mapped[int] = mapped_column(Integer, default=100000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    key_id: Mapped[str] = mapped_column(String(36))
    period: Mapped[str] = mapped_column(String(7), index=True)
    amount_micros: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Pending reservations deliberately remain charged after crash until explicit reconciliation.


class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    reservation_id: Mapped[str] = mapped_column(String(36), unique=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
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
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class OIDCState(Base):
    __tablename__ = "oidc_states"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(128))
    verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    csrf: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
        kwargs.update(pool_size=10, max_overflow=20, isolation_level="READ COMMITTED")
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
