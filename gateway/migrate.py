"""Explicit, serialized versioned schema migration. Never runs on app startup."""
import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, inspect, select, text

from . import db
from .migrations import HEAD, REVISIONS, v0001, v0002, v0003, v0004, v0005, v0006, v0007, v0008, v0009, v0010, v0011

version_metadata = MetaData()
versions = Table("gateway_schema_versions", version_metadata,
                 Column("version", Integer, primary_key=True),
                 Column("applied_at", DateTime(timezone=True), nullable=False))


class MigrationError(RuntimeError):
    pass


def expected_server_default(connection, table, column, actual_default):
    if column.server_default is not None:
        expected = str(column.server_default.arg)
        actual = actual_default or ""
        if connection.dialect.name == "postgresql" and actual.endswith("::json"):
            actual = actual[:-6]
        return actual == expected
    if actual_default is None:
        return True
    # PostgreSQL implements an implicit integer autoincrement PK as SERIAL.
    # Verify the sequence is actually owned by this column, then compare the
    # exact regclass-rendered nextval expression (not merely a string prefix).
    if connection.dialect.name != "postgresql" or table._autoincrement_column is not column:
        return False
    sequence = connection.execute(text(
        "SELECT CAST(CAST(pg_get_serial_sequence(:table_name, :column_name) AS regclass) AS text)"
    ), {"table_name": table.name, "column_name": column.name}).scalar()
    if not sequence:
        return False
    return actual_default == "nextval('" + sequence.replace("'", "''") + "'::regclass)"


def validate_v1(connection):
    validate_schema(connection, v0001.Base.metadata)


def validate_head(connection):
    validate_schema(connection, v0011.metadata)


def validate_schema(connection, expected):
    """Refuse partial or divergent schema, never guess from table names."""
    inspector = inspect(connection)
    found = set(inspector.get_table_names()) - {versions.name}
    if found != set(expected.tables):
        raise MigrationError("Schema table set does not match revision; manual migration required")
    for name, table in expected.tables.items():
        actual = {c["name"]: c for c in inspector.get_columns(name)}
        if set(actual) != set(table.columns.keys()):
            raise MigrationError(f"Schema columns differ in {name}")
        for column in table.columns:
            other = actual[column.name]
            # Exact compiled type also checks string lengths and bigint vs integer.
            if (str(other["type"].compile(dialect=connection.dialect)).upper() !=
                    str(column.type.compile(dialect=connection.dialect)).upper()
                    or other["nullable"] != column.nullable or not expected_server_default(connection, table, column, other.get("default"))):
                raise MigrationError(f"Schema column differs: {name}.{column.name}")
        pk = set(inspector.get_pk_constraint(name).get("constrained_columns") or [])
        if pk != {c.name for c in table.primary_key.columns}:
            raise MigrationError(f"Schema primary key differs: {name}")
        actual_unique = {tuple(sorted(c["column_names"])) for c in inspector.get_unique_constraints(name)}
        expected_unique = {tuple(sorted(c.name for c in constraint.columns)) for constraint in table.constraints
                           if constraint.__class__.__name__ == "UniqueConstraint"}
        if actual_unique != expected_unique:
            raise MigrationError(f"Schema uniqueness differs: {name}")
        actual_fk = {(tuple(c["constrained_columns"]), c["referred_table"], tuple(c["referred_columns"]))
                     for c in inspector.get_foreign_keys(name)}
        expected_fk = {(tuple(c.name for c in constraint.columns), constraint.referred_table.name,
                        tuple(e.column.name for e in constraint.elements)) for constraint in table.foreign_key_constraints}
        if actual_fk != expected_fk:
            raise MigrationError(f"Schema foreign keys differ: {name}")
        actual_indexes = {(tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(name)
                          if not i.get("duplicates_constraint")}
        expected_indexes = {(tuple(c.name for c in i.columns), bool(i.unique)) for i in table.indexes}
        if actual_indexes != expected_indexes:
            raise MigrationError(f"Schema indexes differ: {name}")
    if connection.execute(select(v0001.PolicyLock.id)).scalars().all() != [1]:
        raise MigrationError("Required policy lock missing or invalid")


def _upgrade(connection, adopt_v1=False):
    tables = set(inspect(connection).get_table_names())
    if versions.name not in tables:
        if tables:
            if not adopt_v1:
                raise MigrationError("Unversioned database: back up then use --adopt-v1 after review")
            validate_v1(connection)
            versions.create(connection)
            connection.execute(versions.insert().values(version=1, applied_at=datetime.now(timezone.utc)))
        else:
            versions.create(connection)
    applied = connection.execute(select(versions.c.version).order_by(versions.c.version)).scalars().all()
    if applied != list(range(1, len(applied) + 1)) or (applied and applied[-1] > HEAD):
        raise MigrationError("Unknown or noncontiguous schema history; use matching application release")
    if not applied and set(inspect(connection).get_table_names()) != {versions.name}:
        raise MigrationError("Schema exists without revision history; refusing partial initialization")
    if applied:
        validate_schema(connection, {1: v0001.Base.metadata, 2: v0002.metadata, 3: v0003.metadata, 4: v0004.metadata, 5: v0005.metadata, 6: v0006.metadata, 7: v0007.metadata, 8: v0008.metadata, 9: v0009.metadata, 10: v0010.metadata, 11: v0011.metadata}[applied[-1]])
    for number, revision in REVISIONS:
        if number not in applied:
            revision.upgrade(connection)
            connection.execute(versions.insert().values(version=number, applied_at=datetime.now(timezone.utc)))
    validate_head(connection)
    return HEAD


async def migrate(adopt_v1=False):
    if db.engine is None:
        db.init_engine()
    # SQLite is one-process development only; BEGIN IMMEDIATE also serializes
    # separate local migration processes. PostgreSQL lock is cluster-wide.
    async with db.engine.connect() as conn:
        async with conn.begin():
            if db.engine.dialect.name == "postgresql":
                # Transaction-local override also handles callers reusing a runtime engine.
                settings = db.get_settings()
                await conn.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                                   {"value": str(settings.db_migration_statement_timeout_ms)})
                await conn.execute(text("SELECT set_config('lock_timeout', :value, true)"),
                                   {"value": str(settings.db_migration_lock_timeout_ms)})
                await conn.exec_driver_sql("SELECT pg_advisory_xact_lock(91827364)")
            elif db.engine.dialect.name == "sqlite":
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                raise MigrationError("Only PostgreSQL and development SQLite are supported")
            return await conn.run_sync(lambda sync: _upgrade(sync, adopt_v1))


def downgrade(*args, **kwargs):
    raise MigrationError("Automatic downgrade is disabled: restore a verified backup with its matching release")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adopt-v1", action="store_true", help="Validate and stamp an existing unversioned v1 schema")
    args = parser.parse_args()
    try:
        version = asyncio.run(migrate(adopt_v1=args.adopt_v1))
    except MigrationError as error:
        parser.exit(1, f"Migration refused: {error}\n")
    print(f"Schema is at revision {version}")


if __name__ == "__main__":
    main()
