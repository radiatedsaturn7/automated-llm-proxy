"""Shared provider circuit state; no credentials or prompt data."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, MetaData, String, Table
from . import v0004

metadata = MetaData()
for table in v0004.metadata.sorted_tables:
    table.to_metadata(metadata)
health = Table('provider_health', metadata,
    Column('provider_id', String(36), ForeignKey('providers.id'), primary_key=True),
    Column('model_alias', String(128), primary_key=True),
    Column('state', String(16), nullable=False),
    Column('failures', Integer, nullable=False),
    Column('epoch', Integer, nullable=False),
    Column('open_until', DateTime(timezone=True), nullable=True),
    Column('probe_id', String(36), nullable=True),
    Column('probe_until', DateTime(timezone=True), nullable=True),
    Column('last_outcome', String(32), nullable=True),
    Column('updated_at', DateTime(timezone=True), nullable=False))


def upgrade(connection):
    health.create(connection)
