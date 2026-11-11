"""Explicit secondary memberships; no implicit delegated privilege migration."""
from sqlalchemy import Column, ForeignKey, MetaData, String, Table
from . import v0007

metadata = MetaData()
for table in v0007.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    # Column.index flags can recreate obsolete single-column indexes on copying.
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
memberships = Table(
    "team_memberships", metadata,
    Column("user_id", String(36), ForeignKey("users.id"), primary_key=True),
    Column("team_id", String(36), ForeignKey("teams.id"), primary_key=True),
    Column("role", String(16), nullable=False),
)


def upgrade(connection):
    memberships.create(connection)
