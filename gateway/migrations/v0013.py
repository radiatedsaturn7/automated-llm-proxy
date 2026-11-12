"""Bind supported application writers to the verified primary encryption key."""
from sqlalchemy import Column, MetaData, String, select, update
from sqlalchemy.schema import CreateColumn
from . import v0012

metadata = MetaData()
for table in v0012.metadata.sorted_tables:
    copied = table.to_metadata(metadata)
    names = {index.name for index in table.indexes}
    for index in list(copied.indexes):
        if index.name not in names:
            copied.indexes.remove(index)
column = Column("master_key_fingerprint", String(64), nullable=True)
metadata.tables["policy_lock"].append_column(column)


def upgrade(connection):
    from ..encryption import fingerprint, primary, validate_plaintext
    # Migration advisory lock alone does not exclude supported application writers.
    gate = v0012.metadata.tables["policy_lock"]
    if connection.execute(select(gate.c.id).where(gate.c.id == 1).with_for_update()).scalar() != 1:
        raise ValueError("Policy gate missing")
    decryptor = primary()
    for name, fields, mfa in (("providers", ("encrypted_key",), False), ("local_mfa", ("seed", "pending_seed"), True)):
        table = v0012.metadata.tables[name]
        pk = list(table.primary_key.columns)[0]
        after = None
        while True:
            query = select(pk, *(table.c[f] for f in fields)).order_by(pk).limit(256)
            if after is not None:
                query = query.where(pk > after)
            rows = connection.execute(query).all()
            if not rows:
                break
            for row in rows:
                for value in row[1:]:
                    if value is not None:
                        validate_plaintext(decryptor.decrypt(value.encode()), mfa=mfa)
            after = rows[-1][0]
    ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
    connection.exec_driver_sql(f"ALTER TABLE policy_lock ADD COLUMN {ddl}")
    connection.execute(update(metadata.tables["policy_lock"]).where(metadata.tables["policy_lock"].c.id == 1).values(master_key_fingerprint=fingerprint()))
