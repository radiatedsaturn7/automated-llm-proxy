"""Crash-recoverable agent root rotation and access-token generation fencing."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, MetaData, String, Table, text
from sqlalchemy.schema import CreateColumn
from . import v0005

metadata = MetaData()
for table in v0005.metadata.sorted_tables:
    table.to_metadata(metadata)
key = metadata.tables["api_keys"]
key.append_column(Column("generation", Integer, nullable=False, server_default=text("0")))
key.append_column(Column("last_rotation_operation", String(36), nullable=True))
Table("agent_rotations", metadata,
      Column("key_id", String(36), ForeignKey("api_keys.id"), primary_key=True),
      Column("operation_id", String(36), nullable=False),
      Column("pending_digest", String(64), unique=True, nullable=False),
      Column("generation", Integer, nullable=False), Column("expires_at", DateTime(timezone=True), nullable=False))


def upgrade(connection):
    for name in ("generation", "last_rotation_operation"):
        ddl = str(CreateColumn(key.c[name]).compile(dialect=connection.dialect))
        connection.exec_driver_sql(f"ALTER TABLE api_keys ADD COLUMN {ddl}")
    metadata.tables["agent_rotations"].create(connection)
