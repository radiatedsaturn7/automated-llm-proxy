"""Immutable initial schema. Never import live models or edit after release.

Future changes belong in new ordered revisions with explicit upgrade operations.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    budget_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"
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


def upgrade(connection):
    Base.metadata.create_all(connection, checkfirst=False)
    connection.execute(PolicyLock.__table__.insert().values(id=1))
