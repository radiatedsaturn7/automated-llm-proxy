"""Index member history by owner and settlement timestamp for bounded window reads."""
from sqlalchemy import Index, MetaData
from . import v0015

metadata = MetaData()
for table in v0015.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
usage = metadata.tables["usage"]
history_index = Index("ix_usage_user_created_id", usage.c.user_id, usage.c.created_at, usage.c.id)


def upgrade(connection):
    history_index.create(connection)
