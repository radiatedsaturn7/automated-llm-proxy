"""Composite query paths for budget admission, exports and credential lifecycle.

Ordinary transactional index creation: schedule a maintenance window, not online.
"""
from sqlalchemy import Index, MetaData
from . import v0006

metadata = MetaData()
for table in v0006.metadata.sorted_tables:
    table.to_metadata(metadata)

DROP = {
    "usage": ("ix_usage_user_id", "ix_usage_team_id"),
    "reservations": ("ix_reservations_user_id", "ix_reservations_team_id"),
    "audit": ("ix_audit_created_at",),
}
ADD = {
    "usage": (("ix_usage_user_period", "user_id", "period"), ("ix_usage_team_period", "team_id", "period"),
              ("ix_usage_created_id", "created_at", "id")),
    "reservations": (("ix_reservations_user_period_status", "user_id", "period", "status"),
                     ("ix_reservations_team_period_status", "team_id", "period", "status")),
    "audit": (("ix_audit_created_id", "created_at", "id"),),
    "api_keys": (("ix_api_keys_user_created_id", "user_id", "created_at", "id"),),
    "browser_sessions": (("ix_browser_sessions_expiry_digest", "expires_at", "digest"),
                         ("ix_browser_sessions_user", "user_id")),
    "oidc_states": (("ix_oidc_states_expiry_digest", "expires_at", "digest"),),
}
for name, indexes in DROP.items():
    table = metadata.tables[name]
    for index in list(table.indexes):
        if index.name in indexes:
            table.indexes.remove(index)
for name, specs in ADD.items():
    for index_name, *columns in specs:
        Index(index_name, *(metadata.tables[name].c[column] for column in columns))


def upgrade(connection):
    # Build replacements first. Failure rolls back both indexes and ledger.
    for name, specs in ADD.items():
        for index_name, *_ in specs:
            next(index for index in metadata.tables[name].indexes if index.name == index_name).create(connection)
    for name, indexes in DROP.items():
        for index in v0006.metadata.tables[name].indexes:
            if index.name in indexes:
                index.drop(connection)
