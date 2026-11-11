"""Least-privilege credentials with explicit backward-compatible legacy backfill."""
from sqlalchemy import Column, JSON, MetaData, text
from sqlalchemy.schema import CreateColumn
from . import v0003

metadata = MetaData()
for table in v0003.metadata.sorted_tables:
    table.to_metadata(metadata)
permissions = Column("permissions", JSON, nullable=False, server_default=text("'[\"inference\"]'"))
metadata.tables["api_keys"].append_column(permissions)


def upgrade(connection):
    ddl = str(CreateColumn(permissions).compile(dialect=connection.dialect))
    connection.exec_driver_sql(f"ALTER TABLE api_keys ADD COLUMN {ddl}")
    keys, users = metadata.tables["api_keys"], metadata.tables["users"]
    # Existing member API keys historically had inference + self-service access.
    connection.execute(keys.update().where(keys.c.kind == "api").values(permissions=["inference", "account"]))
    connection.execute(keys.update().where(keys.c.kind == "api", keys.c.user_id.in_(
        users.select().with_only_columns(users.c.id).where(users.c.role == "admin"))).values(
            permissions=["admin", "inference", "account", "provisioning"]))
    # Agent credentials remain inference-only regardless of owner's role.
