"""Persist IdP-owned SCIM external IDs; additive, nullable and unique per resource."""
from sqlalchemy import Column, Index, MetaData, String
from sqlalchemy.schema import CreateColumn

from . import v0001

metadata = MetaData()
for table in v0001.Base.metadata.sorted_tables:
    table.to_metadata(metadata)
for name in ("teams", "users"):
    column = Column("scim_external_id", String(255), nullable=True)
    metadata.tables[name].append_column(column)
    Index(f"ix_{name}_scim_external_id", column, unique=True)


def upgrade(connection):
    for name in ("teams", "users"):
        table = metadata.tables[name]
        column = table.c.scim_external_id
        ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
        connection.exec_driver_sql(f"ALTER TABLE {name} ADD COLUMN {ddl}")
        next(index for index in table.indexes if index.name == f"ix_{name}_scim_external_id").create(connection)
