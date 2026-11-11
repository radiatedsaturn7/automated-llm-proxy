"""Access-path assertions; PG plans run only against disposable TEST_DATABASE_URL."""
import json

import pytest
from sqlalchemy import inspect, text

from gateway import db
from gateway.migrations.v0007 import ADD, DROP


async def test_composite_paths_present_without_redundant_prefix_indexes(client):
    async with db.engine.connect() as connection:
        indexes = await connection.run_sync(lambda conn: {
            name: {index["name"]: tuple(index["column_names"]) for index in inspect(conn).get_indexes(name)}
            for name in ADD})
    for table, definitions in ADD.items():
        for name, *columns in definitions:
            assert indexes[table][name] == tuple(columns)
    for table, names in DROP.items():
        assert not set(names).intersection(indexes[table])


def index_names(plan):
    names = {plan["Index Name"]} if "Index Name" in plan else set()
    for child in plan.get("Plans", []):
        names |= index_names(child)
    return names


async def test_postgres_selective_budget_and_export_plans(client):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Requires TEST_DATABASE_URL for real PostgreSQL planner evidence")
    async with db.engine.begin() as connection:
        # 20k entries across 1000 subjects, 12 months and three statuses. This
        # tests the production predicates with genuine ANALYZE statistics; no
        # enable_seqscan overrides or forced planner hints.
        await connection.execute(text("""
            INSERT INTO usage (id,reservation_id,user_id,team_id,key_id,model,provider_id,period,
                               input_tokens,output_tokens,cost_micros,estimated,created_at)
            SELECT md5('usage'||i), md5('reservation'||i), (i%1000)::text, (i%1000)::text,
                   'key', 'model', 'provider', '2026-'||lpad(((i/1000)%12+1)::text,2,'0'),
                   10,5,15,false, TIMESTAMPTZ '2026-01-01' + i * INTERVAL '1 second'
            FROM generate_series(1,20000) AS i
        """))
        await connection.execute(text("""
            INSERT INTO reservations (id,user_id,team_id,key_id,period,amount_micros,status,created_at)
            SELECT md5('reservation'||i), (i%1000)::text, (i%1000)::text, 'key',
                   '2026-'||lpad(((i/1000)%12+1)::text,2,'0'), 15,
                   CASE WHEN i%3=0 THEN 'pending' WHEN i%3=1 THEN 'settled' ELSE 'released' END,
                   TIMESTAMPTZ '2026-01-01' + i * INTERVAL '1 second'
            FROM generate_series(1,20000) AS i
        """))
        await connection.execute(text("""
            INSERT INTO audit (id,actor_id,action,details,created_at)
            SELECT md5('audit'||i), 'actor', 'test', '{}',
                   TIMESTAMPTZ '2026-01-01' + i * INTERVAL '1 second'
            FROM generate_series(1,20000) AS i
        """))
        for table in ("usage", "reservations", "audit"):
            await connection.execute(text(f"ANALYZE {table}"))
        queries = []
        for subject in ("user", "team"):
            queries.append((f"SELECT sum(cost_micros) FROM usage WHERE {subject}_id='42' AND period='2026-01'",
                            f"ix_usage_{subject}_period"))
            queries.append((f"SELECT sum(amount_micros) FROM reservations WHERE {subject}_id='42' AND period='2026-01' AND status='pending'",
                            f"ix_reservations_{subject}_period_status"))
        for table in ("usage", "audit"):
            queries.append((f"SELECT * FROM {table} WHERE created_at >= TIMESTAMPTZ '2026-01-01 05:00:00+00' "
                            "AND created_at < TIMESTAMPTZ '2026-01-02' "
                            "AND (created_at,id) > (TIMESTAMPTZ '2026-01-01 05:01:00+00', 'cursor') "
                            "ORDER BY created_at,id LIMIT 21", f"ix_{table}_created_id"))
        for query, expected_index in queries:
            plan = (await connection.execute(text("EXPLAIN (FORMAT JSON) " + query))).scalar()
            if isinstance(plan, str):
                plan = json.loads(plan)
            assert expected_index in index_names(plan[0]["Plan"]), plan
