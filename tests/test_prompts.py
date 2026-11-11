import asyncio
import json

from sqlalchemy import select

from gateway import db

BASE = "/admin/api/prompt-registry"


async def create(client, headers, content="Hello {{name}}", name="greeting"):
    response = await client.post(BASE + "/templates", headers=headers, json={"name": name, "content": content})
    assert response.status_code == 201, response.text
    return response.json()


async def test_registry_admin_only(client, admin_headers):
    assert (await client.get(BASE + "/templates")).status_code == 401
    user = (await client.post("/admin/api/users", headers=admin_headers, json={"email": "member@example.com"})).json()
    key = (await client.post("/admin/api/keys", headers=admin_headers, json={"name": "member", "user_id": user["id"]})).json()
    assert (await client.post(BASE + "/templates", headers={"Authorization": "Bearer " + key["key"]},
                              json={"name": "unauthorized", "content": "text"})).status_code == 403


async def test_immutable_versions_and_archive(client, admin_headers):
    result = await create(client, admin_headers)
    path = BASE + "/templates/" + result["template"]["id"]
    async def append(index):
        return await client.post(path + "/versions", headers=admin_headers, json={"content": f"Version {index}"})
    responses = await asyncio.gather(*(append(index) for index in range(5)))
    assert all(r.status_code == 201 for r in responses)
    assert sorted(r.json()["version"] for r in responses) == [2, 3, 4, 5, 6]
    first = (await client.get(path + "/versions/1", headers=admin_headers)).json()
    assert first["content"] == "Hello {{name}}"
    assert (await client.patch(path + "/versions/1", headers=admin_headers, json={"content": "overwrite"})).status_code == 405
    assert (await client.delete(path, headers=admin_headers)).status_code == 405
    assert (await client.patch(path, headers=admin_headers, json={"archived": True})).status_code == 200
    assert (await append(7)).status_code == 409
    assert (await client.get(path + "/versions/1", headers=admin_headers)).status_code == 200
    assert (await client.patch(path, headers=admin_headers, json={"archived": False})).status_code == 200
    assert (await append(7)).status_code == 201


async def test_literal_render_no_execution_or_recursive_substitution(client, admin_headers):
    result = await create(client, admin_headers)
    path = BASE + "/templates/" + result["template"]["id"] + "/versions/1/render"
    value = "{{name}} ${7*7} \\1 __import__('os').system('no')"
    response = await client.post(path, headers=admin_headers, json={"variables": {"name": value}})
    assert response.status_code == 200
    assert response.json()["content"] == "Hello " + value
    for params in [{}, {"wrong": "x"}, {"name": "x", "extra": "y"}]:
        assert (await client.post(path, headers=admin_headers, json={"variables": params})).status_code == 400
    assert (await client.post(BASE + "/templates", headers=admin_headers,
                              json={"name": "unsafe", "content": "{{obj.attribute}}"})).status_code == 422


async def test_render_and_submission_limits(client, admin_headers):
    result = await create(client, admin_headers, "{{name}}" * 20)
    path = BASE + "/templates/" + result["template"]["id"] + "/versions/1/render"
    assert (await client.post(path, headers=admin_headers, json={"variables": {"name": "x" * 8192}})).status_code == 400
    assert (await client.post(path, headers=admin_headers, json={"variables": {"name": "x" * 8193}})).status_code == 422
    assert (await client.post(BASE + "/templates", headers=admin_headers,
                              json={"name": "large", "content": "x" * 32769})).status_code == 422


async def test_evaluation_metadata_persistence_and_no_case_retention(client, admin_headers):
    result = await create(client, admin_headers, "secret-template {{name}}")
    path = BASE + "/templates/" + result["template"]["id"] + "/versions/1/evaluations"
    response = await client.post(path, headers=admin_headers, json={"cases": [
        {"input": "secret-input", "output": "secret-output", "expected": "secret-output"}], "rules": ["exact_match"]})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["prompt_version_id"] == result["version"]["id"]
    assert run["report"]["pass_rate"] == 1
    assert "secret-" not in response.text
    assert (await client.get(BASE + "/evaluations/" + run["id"], headers=admin_headers)).json() == run
    listed = (await client.get(BASE + "/evaluations", headers=admin_headers,
                               params={"prompt_version_id": result["version"]["id"]})).json()
    assert len(listed) == 1
    async with db.session_factory() as session:
        stored = await session.get(db.EvaluationRun, run["id"])
        assert "secret-" not in json.dumps(stored.report)
        audit = (await session.scalars(select(db.Audit))).all()
        assert all("secret-" not in json.dumps(row.details) for row in audit)
    assert (await client.post(path, headers=admin_headers, json={"cases": [{"output": "x"}],
                                                               "rules": ["exact_match"]})).status_code == 400


async def test_missing_references_and_duplicate_names(client, admin_headers):
    await create(client, admin_headers)
    assert (await client.post(BASE + "/templates", headers=admin_headers,
                              json={"name": "greeting", "content": "duplicate"})).status_code == 409
    assert (await client.get(BASE + "/templates/missing/versions/1", headers=admin_headers)).status_code == 404
    assert (await client.post(BASE + "/templates/missing/versions/1/evaluations", headers=admin_headers,
                              json={"cases": [{"output": "x"}], "rules": ["nonempty"]})).status_code == 404
