"""Historical team tombstones release live SCIM identifiers without deleting history."""
from sqlalchemy import Column, DateTime, MetaData, String
from sqlalchemy.schema import CreateColumn
from . import v0013

metadata = MetaData()
for table in v0013.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
columns = (
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    Column("deleted_name", String(120), nullable=True),
    Column("deleted_external_id", String(255), nullable=True),
)
for column in columns:
    metadata.tables["teams"].append_column(column)


def upgrade(connection):
    for column in columns:
        ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.exec_driver_sql(f"ALTER TABLE teams ADD COLUMN {ddl}")
