"""Optional browser-approved enrollment; only digests of device credentials."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, JSON, MetaData, String, Table
from sqlalchemy.schema import CreateColumn
from . import v0008

metadata = MetaData()
for table in v0008.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
enrollment_id = Column("enrollment_request_id", String(36), nullable=True)
metadata.tables["api_keys"].append_column(enrollment_id)
Index("ix_api_keys_enrollment_request_id", enrollment_id, unique=True)
enrollments = Table(
    "agent_enrollments", metadata,
    Column("request_id", String(36), primary_key=True),
    Column("approval_code", String(36), unique=True, nullable=False),
    Column("candidate_digest", String(64), unique=True, nullable=False),
    Column("device_name", String(120), nullable=False),
    Column("policy", JSON, nullable=False),
    Column("state", String(16), nullable=False),
    Column("expires_at", DateTime(timezone=True), index=True, nullable=False),
    Column("last_poll", DateTime(timezone=True), nullable=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=True),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=True),
    Column("key_id", String(36), ForeignKey("api_keys.id"), nullable=True),
)


def upgrade(connection):
    ddl = str(CreateColumn(enrollment_id).compile(dialect=connection.dialect))
    connection.exec_driver_sql(f"ALTER TABLE api_keys ADD COLUMN {ddl}")
    next(i for i in metadata.tables["api_keys"].indexes if i.name == "ix_api_keys_enrollment_request_id").create(connection)
    enrollments.create(connection)
