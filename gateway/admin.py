import hmac
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from . import db, schemas
from .config import get_settings
from .security import Identity, admin, encrypt, new_key

router = APIRouter()


def micros(value):
    return None if value is None else int(Decimal(value) * 1_000_000)


def record(item):
    hidden = {"digest", "encrypted_key", "oidc_subject"}
    data = {column.name: getattr(item, column.name) for column in item.__table__.columns if column.name not in hidden}
    if "budget_micros" in data:
        data["budget_usd"] = None if data["budget_micros"] is None else data["budget_micros"] / 1_000_000
    return data


@router.post("/bootstrap", status_code=201)
async def bootstrap(body: schemas.Bootstrap):
    if not hmac.compare_digest(body.token, get_settings().bootstrap_token):
        raise HTTPException(403, "Invalid bootstrap token")
    async with db.transaction() as s:
        if await s.scalar(select(func.count()).select_from(db.User)):
            raise HTTPException(409, "Gateway already initialized")
        body_user = schemas.UserCreate(email=body.email, role="admin")
        user = db.User(email=body_user.email, role="admin")
        s.add(user)
        await s.flush()
        raw, key = new_key(user.id, None, "bootstrap-admin", expires_in_days=7)
        s.add(key)
        await db.audit(s, user.id, "bootstrap", user.id)
        return {"user_id": user.id, "key": raw, "expires_at": key.expires_at}


@router.get("/admin/api/{resource}")
async def list_resource(resource: str, identity: Identity = Depends(admin),
                        limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
    models = {"teams": db.Team, "users": db.User, "providers": db.Provider, "keys": db.APIKey,
              "usage": db.Usage, "audit": db.Audit, "reservations": db.Reservation}
    if resource == "settings":
        cfg = get_settings()
        return {"environment": cfg.environment, "oidc_enabled": bool(cfg.oidc_issuer),
                "password_login": False, "budget_period": "UTC calendar month", "version": "0.1.0"}
    if resource not in models:
        raise HTTPException(404, "Not found")
    model = models[resource]
    async with db.session_factory() as s:
        order = model.created_at.desc() if hasattr(model, "created_at") else model.id
        rows = (await s.scalars(select(model).order_by(order).offset(offset).limit(limit))).all()
        return [record(x) for x in rows]


@router.post("/admin/api/teams", status_code=201)
async def create_team(body: schemas.TeamCreate, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        team = db.Team(name=body.name, budget_micros=micros(body.budget_usd))
        s.add(team)
        await s.flush()
        await db.audit(s, identity.user.id, "team.create", team.id)
        return record(team)


@router.patch("/admin/api/teams/{team_id}")
async def patch_team(team_id: str, body: schemas.TeamPatch, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        team = await s.get(db.Team, team_id)
        if not team:
            raise HTTPException(404, "Team not found")
        if "budget_usd" in body.model_fields_set:
            team.budget_micros = micros(body.budget_usd)
        if body.enabled is not None:
            team.enabled = body.enabled
        await db.audit(s, identity.user.id, "team.update", team.id)
        return record(team)


@router.post("/admin/api/users", status_code=201)
async def create_user(body: schemas.UserCreate, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        if body.team_id and not await s.get(db.Team, body.team_id):
            raise HTTPException(404, "Team not found")
        user = db.User(email=body.email, team_id=body.team_id, role=body.role, budget_micros=micros(body.budget_usd))
        s.add(user)
        await s.flush()
        await db.audit(s, identity.user.id, "user.create", user.id)
        return record(user)


@router.patch("/admin/api/users/{user_id}")
async def patch_user(user_id: str, body: schemas.UserPatch, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        user = await s.get(db.User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        if user_id == identity.user.id and (body.enabled is False or body.role == "member"):
            raise HTTPException(400, "Cannot disable or demote own administrator account")
        if "budget_usd" in body.model_fields_set:
            user.budget_micros = micros(body.budget_usd)
        if body.enabled is not None:
            user.enabled = body.enabled
        if body.role:
            user.role = body.role
        await db.audit(s, identity.user.id, "user.update", user.id)
        return record(user)


@router.post("/admin/api/providers", status_code=201)
async def create_provider(body: schemas.ProviderCreate, identity: Identity = Depends(admin)):
    from .providers import validate_base_url
    await validate_base_url(body.base_url)
    async with db.transaction() as s:
        provider = db.Provider(name=body.name, kind=body.kind, base_url=body.base_url.rstrip("/"),
                               encrypted_key=encrypt(body.api_key), models=body.model_dump(mode="json")["models"],
                               enabled=body.enabled, priority=body.priority)
        s.add(provider)
        await s.flush()
        await db.audit(s, identity.user.id, "provider.create", provider.id)
        return record(provider)


@router.patch("/admin/api/providers/{provider_id}")
async def patch_provider(provider_id: str, body: schemas.ProviderPatch, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        provider = await s.get(db.Provider, provider_id)
        if not provider:
            raise HTTPException(404, "Provider not found")
        if body.enabled is not None:
            provider.enabled = body.enabled
        if body.api_key:
            provider.encrypted_key = encrypt(body.api_key)
        if body.models is not None:
            if not body.models:
                raise HTTPException(400, "At least one model required")
            provider.models = body.model_dump(mode="json")["models"]
        if body.priority is not None:
            provider.priority = body.priority
        await db.audit(s, identity.user.id, "provider.update", provider.id)
        return record(provider)


@router.post("/admin/api/keys", status_code=201)
async def create_key(body: schemas.KeyCreate, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        user = await s.get(db.User, body.user_id)
        if not user or not user.enabled:
            raise HTTPException(404, "Active user not found")
        if body.team_id is not None and body.team_id != user.team_id:
            raise HTTPException(400, "Key team must match user team")
        raw, key = new_key(user.id, user.team_id, body.name, body.expires_in_days, body.kind,
                           allowed_models=body.allowed_models, rpm=body.rpm, tpm=body.tpm)
        s.add(key)
        await s.flush()
        await db.audit(s, identity.user.id, "key.create", key.id, kind=key.kind)
        return {**record(key), "key": raw}


@router.post("/admin/api/keys/{key_id}/revoke")
async def revoke_key(key_id: str, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        key = await s.get(db.APIKey, key_id)
        if not key:
            raise HTTPException(404, "Key not found")
        key.revoked = True
        await db.audit(s, identity.user.id, "key.revoke", key.id)
        return {"revoked": True}


@router.post("/admin/api/keys/{key_id}/rotate")
async def rotate_key(key_id: str, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        key = await s.get(db.APIKey, key_id)
        if not key or key.revoked:
            raise HTTPException(404, "Active key not found")
        from .security import aware
        if aware(key.expires_at) <= db.now():
            raise HTTPException(400, "Cannot rotate expired key")
        raw, replacement = new_key(key.user_id, key.team_id, key.name, kind=key.kind,
                                    allowed_models=key.allowed_models, rpm=key.rpm, tpm=key.tpm)
        replacement.expires_at = key.expires_at  # Rotation never silently extends authorization.
        key.revoked = True
        s.add(replacement)
        await s.flush()
        await db.audit(s, identity.user.id, "key.rotate", key.id, replacement_id=replacement.id)
        return {**record(replacement), "key": raw}


@router.post("/admin/api/evaluate")
async def evaluate_prompt(body: schemas.EvaluationRequest, identity: Identity = Depends(admin)):
    from .evaluation import evaluate
    try:
        result = evaluate(body.cases, body.rules)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    async with db.transaction() as s:
        await db.audit(s, identity.user.id, "evaluation.run", cases=len(body.cases), rules=body.rules)
    return result


@router.post("/admin/api/reservations/{reservation_id}/reconcile")
async def reconcile(reservation_id: str, body: dict, identity: Identity = Depends(admin)):
    """Release a crashed reservation only after an operator confirms provider billing."""
    if body != {"release": True, "provider_billing_checked": True}:
        raise HTTPException(400, "Explicit release and provider_billing_checked required")
    async with db.transaction() as s:
        reservation = await s.get(db.Reservation, reservation_id)
        if not reservation or reservation.status != "pending":
            raise HTTPException(409, "No pending reservation")
        reservation.status = "released"
        await db.audit(s, identity.user.id, "reservation.release", reservation.id)
    return {"released": True}
