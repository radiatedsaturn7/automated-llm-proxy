"""Opt-in local credentials and bounded shared login throttling."""
from sqlalchemy import Boolean, text, Column, DateTime, ForeignKey, Integer, MetaData, String, Table
from sqlalchemy.schema import CreateColumn
from . import v0009
metadata = MetaData()
for table in v0009.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
managed = Column("scim_managed", Boolean, nullable=False, server_default=text("false"))
metadata.tables["users"].append_column(managed)
passwords = Table("local_passwords", metadata,
    Column("user_id", String(36), ForeignKey("users.id"), primary_key=True),
    Column("password_hash", String(256), nullable=False))
throttles = Table("login_throttles", metadata,
    Column("bucket", String(32), primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("attempts", Integer, nullable=False))


def upgrade(connection):
    ddl = str(CreateColumn(managed).compile(dialect=connection.dialect))
    connection.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {ddl}")
    # Legacy SCIM origin is unknowable without externalId/retained audits. Fail closed.
    connection.exec_driver_sql("UPDATE users SET scim_managed = true")
    passwords.create(connection)
    throttles.create(connection)
