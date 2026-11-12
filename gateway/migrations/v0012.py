"""Local TOTP factor and purpose-bound short-lived challenges."""

from sqlalchemy import (
    Boolean,
    Column,
    MetaData,
    Table,
    ForeignKey,
    String,
    Text,
    BigInteger,
    Integer,
    DateTime,
)
from sqlalchemy.schema import CreateColumn
from . import v0011

metadata = MetaData()
for table in v0011.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
column = Column("mfa_verified", Boolean, nullable=True)
metadata.tables["browser_sessions"].append_column(column)
factors = Table(
    "local_mfa",
    metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("seed", Text, nullable=True),
    Column("last_counter", BigInteger, nullable=False),
    Column("grant_digest", String(64), nullable=True),
    Column("grant_expires", DateTime(timezone=True), nullable=True),
    Column("challenge_digest", String(64), nullable=True, unique=True),
    Column("challenge_expires", DateTime(timezone=True), nullable=True),
    Column("pending_seed", Text, nullable=True),
    Column("password_binding", String(64), nullable=True),
    Column("attempts", Integer, nullable=False),
)


def upgrade(connection):
    ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
    connection.exec_driver_sql(f"ALTER TABLE browser_sessions ADD COLUMN {ddl}")
    factors.create(connection)
