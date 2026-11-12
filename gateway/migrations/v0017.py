"""Bounded member key policies and immutable issuance operation receipts."""
from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, JSON, MetaData, String, Table, text
from sqlalchemy.schema import CreateColumn
from . import v0016

metadata = MetaData()
for table in v0016.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
columns = (
    Column("self_service", Boolean, nullable=False, server_default=text("false")),
    Column("issuance_operation_id", String(36), nullable=True),
)
for column in columns:
    metadata.tables["api_keys"].append_column(column)
operation_index = Index("ix_api_keys_issuance_operation_id", metadata.tables["api_keys"].c.issuance_operation_id, unique=True)
policies = Table("user_key_policies", metadata,
    Column("user_id", String(36), ForeignKey("users.id"), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=True),
    Column("allowed_models", JSON, nullable=False),
    Column("permissions", JSON, nullable=False),
    Column("expires_in_days", Integer, nullable=False),
    Column("rpm", Integer, nullable=False),
    Column("tpm", Integer, nullable=False),
    Column("max_active_keys", Integer, nullable=False),
    Column("max_total_keys", Integer, nullable=False))


def upgrade(connection):
    for column in columns:
        ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.exec_driver_sql(f"ALTER TABLE api_keys ADD COLUMN {ddl}")
    operation_index.create(connection)
    policies.create(connection)
