"""OIDC session correlation and shared back-channel logout replay protection."""
from sqlalchemy import Column, DateTime, Integer, Index, MetaData, String, Table
from sqlalchemy.schema import CreateColumn
from . import v0010

metadata = MetaData()
for table in v0010.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
columns = [Column(name, String(512), nullable=True) for name in ("oidc_issuer", "oidc_subject", "oidc_sid")]
for column in columns:
    metadata.tables["browser_sessions"].append_column(column)
session_indexes = [Index("ix_browser_sessions_oidc_sid", metadata.tables["browser_sessions"].c.oidc_issuer,
                         metadata.tables["browser_sessions"].c.oidc_sid),
                   Index("ix_browser_sessions_oidc_subject", metadata.tables["browser_sessions"].c.oidc_issuer,
                         metadata.tables["browser_sessions"].c.oidc_subject)]
replays = Table("oidc_logout_replays", metadata,
    Column("digest", String(64), primary_key=True),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("issuer", String(512), nullable=False),
    Column("subject", String(512), nullable=True),
    Column("sid", String(512), nullable=True),
    Column("issued_at", Integer, nullable=False))
Index("ix_oidc_logout_replays_issuer", replays.c.issuer)


def upgrade(connection):
    for column in columns:
        ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.exec_driver_sql(f"ALTER TABLE browser_sessions ADD COLUMN {ddl}")
    # Existing sessions cannot be securely correlated; require reauthentication.
    connection.exec_driver_sql("DELETE FROM browser_sessions")
    for index in session_indexes:
        index.create(connection)
    replays.create(connection)
