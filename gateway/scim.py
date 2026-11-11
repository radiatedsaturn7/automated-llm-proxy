"""Conservative SCIM 2.0 provisioning subset. One group (team) per user."""

import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from . import db
from .security import Identity, admin
from .schemas import UserCreate

PREFIX = "urn:ietf:params:scim:schemas:"
USER = PREFIX + "core:2.0:User"
GROUP = PREFIX + "core:2.0:Group"
LIST = PREFIX + "api:messages:2.0:ListResponse"
ERROR = PREFIX + "api:messages:2.0:Error"
MEDIA = "application/scim+json"


class SCIMRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                response = await handler(request)
                response.headers["content-type"] = MEDIA
                return response
            except HTTPException as exc:
                return JSONResponse(
                    {"schemas": [ERROR], "status": str(exc.status_code), "detail": str(exc.detail)},
                    status_code=exc.status_code,
                    media_type=MEDIA,
                    headers=exc.headers,
                )
            except IntegrityError:
                return JSONResponse(
                    {"schemas": [ERROR], "status": "409", "detail": "Resource already exists"},
                    status_code=409,
                    media_type=MEDIA,
                )

        return wrapped


router = APIRouter(prefix="/scim/v2", route_class=SCIMRoute, dependencies=[Depends(admin)])


def fail(message, status=400):
    raise HTTPException(status, message)


async def payload(request, allowed):
    try:
        body = await request.json()
    except ValueError:
        fail("Invalid JSON")
    if not isinstance(body, dict) or set(body) - allowed:
        fail("Unsupported attributes; see supported SCIM subset")
    return body


def user_resource(user):
    return {
        "schemas": [USER],
        "id": user.id,
        "userName": user.email,
        "active": user.enabled,
        **({"externalId": user.scim_external_id} if user.scim_external_id is not None else {}),
        "meta": {"resourceType": "User", "location": f"/scim/v2/Users/{user.id}"},
    }


async def group_resource(session, group):
    members = (
        await session.scalars(
            select(db.User).where(db.User.team_id == group.id).order_by(db.User.id).limit(1001)
        )
    ).all()
    if len(members) > 1000:
        fail("Group exceeds supported 1000-member response limit", 413)
    return {
        "schemas": [GROUP],
        "id": group.id,
        "displayName": group.name,
        **({"externalId": group.scim_external_id} if group.scim_external_id is not None else {}),
        "members": [{"value": u.id, "display": u.email} for u in members],
        "meta": {"resourceType": "Group", "location": f"/scim/v2/Groups/{group.id}"},
    }


@router.get("/ServiceProviderConfig")
async def service_config():
    return {
        "schemas": [PREFIX + "core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 100},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Administrator bearer credential",
                "description": "Provision with a restricted-lifetime gateway administrator API key",
            }
        ],
    }


def discovery_resources(kind):
    if kind == "ResourceTypes":
        return [
            {
                "schemas": [PREFIX + "core:2.0:ResourceType"],
                "id": name,
                "name": name,
                "endpoint": "/" + name + "s",
                "schema": schema,
                "schemaExtensions": [],
            }
            for name, schema in [("User", USER), ("Group", GROUP)]
        ]
    if kind == "Schemas":

        def attribute(name, data_type="string", required=False, multi=False, sub=None):
            result = {
                "name": name,
                "type": data_type,
                "multiValued": multi,
                "required": required,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "none",
            }
            if data_type == "string":
                result["caseExact"] = name != "userName"
            if sub:
                result["subAttributes"] = sub
            return result

        username = attribute("userName", required=True)
        username["uniqueness"] = "server"
        return [
            {
                "schemas": [PREFIX + "core:2.0:Schema"],
                "id": USER,
                "name": "User",
                "description": "Thunderbolt supported User attribute subset; userName must be email",
                "attributes": [username, attribute("active", "boolean")],
            },
            {
                "schemas": [PREFIX + "core:2.0:Schema"],
                "id": GROUP,
                "name": "Group",
                "description": "Thunderbolt single-team Group subset; at most 1000 members",
                "attributes": [
                    attribute("displayName", required=True),
                    attribute(
                        "members",
                        "complex",
                        multi=True,
                        sub=[
                            attribute("value", required=True),
                            attribute("display"),
                            attribute("$ref", "reference"),
                        ],
                    ),
                ],
            },
        ]
    return None


@router.get("/{kind}")
async def list_resources(kind: str, request: Request):
    definitions = discovery_resources(kind)
    if definitions is not None:
        if request.query_params:
            fail("Discovery collections do not support query parameters")
        return {
            "schemas": [LIST],
            "totalResults": len(definitions),
            "startIndex": 1,
            "itemsPerPage": len(definitions),
            "Resources": definitions,
        }
    if kind not in {"Users", "Groups"}:
        fail("Unknown resource type", 404)
    model = db.User if kind == "Users" else db.Team
    try:
        start = int(request.query_params.get("startIndex", "1"))
        count = int(request.query_params.get("count", "100"))
    except ValueError:
        fail("Invalid pagination")
    if start < 1 or count < 0 or count > 100:
        fail("startIndex must be positive and count between 0 and 100")
    query = select(model)
    if request.query_params.get("filter"):
        match = re.fullmatch(
            r'(userName|displayName|externalId) eq "([^"\\]{1,320})"', request.query_params["filter"]
        )
        attr = "userName" if kind == "Users" else "displayName"
        if not match or match[1] not in {attr, "externalId"}:
            fail("Only exact userName/displayName/externalId equality filters supported")
        value = match[2].lower() if match[1] == "userName" else match[2]
        column = (
            model.scim_external_id
            if match[1] == "externalId"
            else (model.email if kind == "Users" else model.name)
        )
        query = query.where(column == value)
    if set(request.query_params) - {"filter", "startIndex", "count"}:
        fail("Unsupported query parameter")
    async with db.session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        values = (await session.scalars(query.order_by(model.id).offset(start - 1).limit(count))).all()
        resources = [
            user_resource(u) if kind == "Users" else await group_resource(session, u) for u in values
        ]
    return {
        "schemas": [LIST],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


@router.get("/{kind}/{resource_id}")
async def get_resource(kind: str, resource_id: str):
    definitions = discovery_resources(kind)
    if definitions is not None:
        match = next((item for item in definitions if item["id"] == resource_id), None)
        if match is None:
            fail("Resource not found", 404)
        return match
    if kind not in {"Users", "Groups"}:
        fail("Unknown resource type", 404)
    async with db.session_factory() as session:
        resource = await session.get(db.User if kind == "Users" else db.Team, resource_id)
        if not resource:
            fail("Resource not found", 404)
        return user_resource(resource) if kind == "Users" else await group_resource(session, resource)


def external(value):
    if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 255):
        fail("externalId must be a nonempty string up to 255 characters")
    return value


def email(value):
    try:
        return UserCreate(email=value).email
    except ValueError:
        fail("userName must be an email address")


@router.post("/Users", status_code=201)
async def create_user(request: Request, identity: Identity = Depends(admin)):
    body = await payload(request, {"schemas", "userName", "active", "externalId"})
    if body.get("schemas") != [USER] or not isinstance(body.get("active", True), bool):
        fail("Invalid user schema or active attribute")
    name = email(body.get("userName"))
    external_id = external(body.get("externalId"))
    async with db.transaction() as session:
        user = db.User(
            email=name, enabled=body.get("active", True), role="member", scim_external_id=external_id
        )
        session.add(user)
        await session.flush()
        await db.audit(session, identity.user.id, "scim.user.create", user.id)
        return user_resource(user)


@router.patch("/Users/{resource_id}")
async def patch_user(resource_id: str, request: Request, identity: Identity = Depends(admin)):
    body = await payload(request, {"schemas", "Operations"})
    ops = body.get("Operations")
    if (
        body.get("schemas") != [PREFIX + "api:messages:2.0:PatchOp"]
        or not isinstance(ops, list)
        or not 1 <= len(ops) <= 20
    ):
        fail("Invalid patch operations")
    changes = {}
    for op in ops:
        if (
            not isinstance(op, dict)
            or set(op) - {"op", "path", "value"}
            or str(op.get("op")).lower() != "replace"
            or (op.get("path") is not None and not isinstance(op["path"], str))
        ):
            fail("Only replace operations supported")
        values = {op["path"]: op.get("value")} if op.get("path") else op.get("value")
        if not isinstance(values, dict) or set(values) - {"active", "userName", "externalId"}:
            fail("Only active/userName/externalId changes supported")
        changes.update(values)
    if "active" in changes and not isinstance(changes["active"], bool):
        fail("active must be boolean")
    if "userName" in changes:
        changes["userName"] = email(changes["userName"])
    if "externalId" in changes:
        changes["externalId"] = external(changes["externalId"])
    async with db.transaction() as session:
        user = await session.get(db.User, resource_id)
        if not user:
            fail("Resource not found", 404)
        if user.role == "admin":
            fail("Administrator identities must be managed through administration", 403)
        if "userName" in changes:
            user.email = changes["userName"]
        if "externalId" in changes:
            user.scim_external_id = changes["externalId"]
        if "active" in changes:
            user.enabled = changes["active"]
            if not user.enabled:
                await session.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == user.id))
                await session.execute(
                    update(db.APIKey).where(db.APIKey.user_id == user.id).values(revoked=True)
                )
        await db.audit(session, identity.user.id, "scim.user.update", user.id, fields=sorted(changes))
        return user_resource(user)


@router.delete("/Users/{resource_id}", status_code=204)
async def disable_user(resource_id: str, identity: Identity = Depends(admin)):
    async with db.transaction() as session:
        user = await session.get(db.User, resource_id)
        if not user:
            fail("Resource not found", 404)
        if user.role == "admin":
            fail("Cannot deprovision an administrator through SCIM", 403)
        user.enabled = False
        await session.execute(update(db.APIKey).where(db.APIKey.user_id == user.id).values(revoked=True))
        await session.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == user.id))
        await db.audit(session, identity.user.id, "scim.user.deactivate", user.id)
    return Response(status_code=204)


async def set_members(session, group, members):
    if not isinstance(members, list) or len(members) > 1000:
        fail("members must be a list of up to 1000 users")
    ids = []
    for item in members:
        if (
            not isinstance(item, dict)
            or set(item) - {"value", "display", "$ref"}
            or not isinstance(item.get("value"), str)
        ):
            fail("Invalid group member")
        ids.append(item["value"])
    users = (await session.scalars(select(db.User).where(db.User.id.in_(ids)))).all() if ids else []
    if len(users) != len(set(ids)):
        fail("Unknown group member")
    if any(u.role == "admin" or u.team_id not in {None, group.id} for u in users):
        fail("Users can belong to one team; administrators cannot be assigned by SCIM", 409)
    current = (await session.scalars(select(db.User).where(db.User.team_id == group.id))).all()
    if any(u.role == "admin" for u in current):
        fail("Administrator teams must be managed through administration", 403)
    changed = {u.id for u in current if u.id not in ids} | {u.id for u in users if u.team_id != group.id}
    for user in current:
        if user.id not in ids:
            user.team_id = None
    for user in users:
        user.team_id = group.id
    if changed:
        # Revoke, rather than relying only on team binding: remove/re-add must not revive keys.
        await session.execute(update(db.APIKey).where(db.APIKey.user_id.in_(changed)).values(revoked=True))
        await session.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id.in_(changed)))
    await session.flush()


@router.post("/Groups", status_code=201)
async def create_group(request: Request, identity: Identity = Depends(admin)):
    body = await payload(request, {"schemas", "displayName", "members", "externalId"})
    name = body.get("displayName")
    if body.get("schemas") != [GROUP] or not isinstance(name, str) or not 1 <= len(name) <= 120:
        fail("Invalid group schema or displayName")
    async with db.transaction() as session:
        group = db.Team(name=name, enabled=True, scim_external_id=external(body.get("externalId")))
        session.add(group)
        await session.flush()
        await set_members(session, group, body.get("members", []))
        await db.audit(session, identity.user.id, "scim.group.create", group.id)
        return await group_resource(session, group)


def patch_operations(body):
    ops = body.get("Operations")
    if (
        body.get("schemas") != [PREFIX + "api:messages:2.0:PatchOp"]
        or not isinstance(ops, list)
        or not 1 <= len(ops) <= 20
    ):
        fail("Between 1 and 20 patch operations required")
    for op in ops:
        if (
            not isinstance(op, dict)
            or set(op) - {"op", "path", "value"}
            or str(op.get("op")).lower() not in {"add", "replace", "remove"}
        ):
            fail("Invalid patch operation")
    return ops


def group_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 120 or not value.strip():
        fail("displayName must contain 1 to 120 characters")
    return value


@router.put("/Users/{resource_id}")
async def replace_user(resource_id: str, request: Request, identity: Identity = Depends(admin)):
    body = await payload(request, {"schemas", "id", "meta", "userName", "active", "externalId"})
    if (
        body.get("schemas") != [USER]
        or body.get("id", resource_id) != resource_id
        or not isinstance(body.get("active", True), bool)
    ):
        fail("Invalid replacement user schema, id or active attribute")
    name, external_id = email(body.get("userName")), external(body.get("externalId"))
    async with db.transaction() as session:
        user = await session.get(db.User, resource_id)
        if not user:
            fail("Resource not found", 404)
        if user.role == "admin":
            fail("Administrator identities must be managed through administration", 403)
        user.email, user.enabled, user.scim_external_id = name, body.get("active", True), external_id
        if not user.enabled:
            await session.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == user.id))
            await session.execute(update(db.APIKey).where(db.APIKey.user_id == user.id).values(revoked=True))
        await db.audit(session, identity.user.id, "scim.user.replace", user.id)
        return user_resource(user)


@router.patch("/Groups/{resource_id}")
async def patch_group(resource_id: str, request: Request, identity: Identity = Depends(admin)):
    body = await payload(request, {"schemas", "Operations"})
    ops = patch_operations(body)
    async with db.transaction() as session:
        group = await session.get(db.Team, resource_id)
        if not group:
            fail("Resource not found", 404)
        if await session.scalar(
            select(db.User.id).where(db.User.team_id == group.id, db.User.role == "admin").limit(1)
        ):
            fail("Administrator teams must be managed through administration", 403)
        for op in ops:
            if op.get("path") is not None and not isinstance(op["path"], str):
                fail("Patch path must be a string")
            action, path, value = op["op"].lower(), op.get("path"), op.get("value")
            if path is None and action in {"add", "replace"}:
                if (
                    not isinstance(value, dict)
                    or not value
                    or set(value) - {"displayName", "externalId", "members"}
                ):
                    fail("Unsupported group attributes")
                changes = value
            else:
                changes = {path: value}
            for attribute, new_value in changes.items():
                if attribute == "displayName" and action in {"add", "replace"}:
                    group.name = group_name(new_value)
                elif attribute == "externalId":
                    group.scim_external_id = None if action == "remove" else external(new_value)
                elif attribute == "members":
                    current = (
                        await session.scalars(select(db.User.id).where(db.User.team_id == group.id))
                    ).all()
                    if action == "replace":
                        await set_members(session, group, new_value)
                    elif action == "add":
                        if not isinstance(new_value, list) or len(new_value) > 1000:
                            fail("members add requires a list of up to 1000 users")
                        new_ids = {
                            item.get("value")
                            for item in new_value
                            if isinstance(item, dict) and isinstance(item.get("value"), str)
                        }
                        await set_members(
                            session, group, [{"value": u} for u in current if u not in new_ids] + new_value
                        )
                    elif "value" in op:
                        fail('Use members[value eq "id"] to remove a specific member')
                    else:
                        await set_members(session, group, [])
                elif isinstance(attribute, str) and action == "remove":
                    match = re.fullmatch(r'members\[value eq "([^"\\]{1,36})"\]', attribute)
                    if not match or "value" in op:
                        fail('Only exact members[value eq "id"] removal supported')
                    current = (
                        await session.scalars(select(db.User.id).where(db.User.team_id == group.id))
                    ).all()
                    await set_members(session, group, [{"value": u} for u in current if u != match[1]])
                else:
                    fail("Unsupported group patch operation")
        await session.flush()
        await db.audit(session, identity.user.id, "scim.group.update", group.id, operations=len(ops))
        return await group_resource(session, group)
