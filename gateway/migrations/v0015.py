"""Optional daily request allowances, independent of immutable spend accounting."""
from sqlalchemy import Column, ForeignKey, Integer, JSON, MetaData, String, Table
from . import v0014

metadata = MetaData()
for table in v0014.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
allowances = Table("user_allowances", metadata,
    Column("user_id", String(36), ForeignKey("users.id"), primary_key=True),
    Column("daily_request_limit", Integer, nullable=True),
    Column("monthly_reset_limit", Integer, nullable=False),
    Column("usage_day", String(10), nullable=True),
    Column("requests_used", Integer, nullable=False),
    Column("reset_month", String(7), nullable=True),
    Column("reset_operations", JSON, nullable=False))


def upgrade(connection):
    allowances.create(connection)
