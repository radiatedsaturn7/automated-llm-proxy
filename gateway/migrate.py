"""Version-one schema initialization. Run once before serving, never per request."""
import asyncio
from sqlalchemy import select
from . import db


async def migrate():
    if db.engine is None:
        db.init_engine()
    async with db.engine.begin() as conn:
        if db.engine.dialect.name == "postgresql":
            await conn.exec_driver_sql("SELECT pg_advisory_xact_lock(91827364)")
        await conn.run_sync(db.Base.metadata.create_all)
        if (await conn.execute(select(db.PolicyLock.id))).first() is None:
            await conn.execute(db.PolicyLock.__table__.insert().values(id=1))


def main():
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
