"""Explicit plaintext prompt registry; no inference interception or paid evaluations."""
import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from . import db
from .evaluation import evaluate
from .security import Identity, admin, aware

router = APIRouter(prefix="/admin/api/prompt-registry", tags=["prompt-registry"])
VARIABLE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]{0,63})\}\}")
MAX_RENDERED = 131072


def variables(content):
    remaining = VARIABLE.sub("", content)
    if "{{" in remaining or "}}" in remaining:
        raise ValueError("Use literal {{identifier}} placeholders only")
    names = sorted(set(VARIABLE.findall(content)))
    if len(names) > 32:
        raise ValueError("At most 32 distinct variables")
    return names


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionBody(Strict):
    content: str = Field(min_length=1, max_length=32768)

    @field_validator("content")
    @classmethod
    def safe_placeholders(cls, content):
        variables(content)
        return content


class CreateBody(VersionBody):
    name: str = Field(min_length=1, max_length=120)


class PatchBody(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


class RenderBody(Strict):
    variables: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("variables")
    @classmethod
    def bound_values(cls, values):
        if any(len(value) > 8192 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) for key, value in values.items()):
            raise ValueError("Invalid variable identifier or value longer than 8192 characters")
        return values


class Case(Strict):
    input: str | None = Field(default=None, max_length=65536)
    expected: str | None = Field(default=None, max_length=65536)
    output: str = Field(max_length=65536)


class RunBody(Strict):
    cases: list[Case] = Field(min_length=1, max_length=100)
    rules: list[Literal["exact_match", "contains", "json_valid", "nonempty"]] = Field(
        default_factory=lambda: ["exact_match"], min_length=1, max_length=4)


def record(row):
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return {key: aware(value) if isinstance(value, datetime) else value for key, value in values.items()}


async def get_version(session, template_id, number):
    row = await session.scalar(select(db.PromptVersion).where(
        db.PromptVersion.template_id == template_id, db.PromptVersion.version == number))
    if row is None:
        raise HTTPException(404, "Prompt version not found")
    return row


@router.post("/templates", status_code=201)
async def create(body: CreateBody, identity: Identity = Depends(admin)):
    async with db.transaction() as session:
        template = db.PromptTemplate(name=body.name, created_by=identity.user.id)
        session.add(template)
        await session.flush()
        version = db.PromptVersion(template_id=template.id, version=1, content=body.content,
                                   variables=variables(body.content), created_by=identity.user.id)
        session.add(version)
        await session.flush()
        await db.audit(session, identity.user.id, "prompt.create", template.id, version=1)
        return {"template": record(template), "version": record(version)}


@router.get("/templates")
async def templates(identity: Identity = Depends(admin), limit: int = Query(100, ge=1, le=100),
                    offset: int = Query(0, ge=0)):
    async with db.session_factory() as session:
        rows = await session.scalars(select(db.PromptTemplate).order_by(db.PromptTemplate.id).limit(limit).offset(offset))
        return [record(row) for row in rows]


@router.get("/templates/{template_id}")
async def get_template(template_id: str, identity: Identity = Depends(admin)):
    async with db.session_factory() as session:
        row = await session.get(db.PromptTemplate, template_id)
        if row is None:
            raise HTTPException(404, "Prompt template not found")
        return record(row)


@router.patch("/templates/{template_id}")
async def patch(template_id: str, body: PatchBody, identity: Identity = Depends(admin)):
    async with db.transaction() as session:
        row = await session.get(db.PromptTemplate, template_id)
        if row is None:
            raise HTTPException(404, "Prompt template not found")
        if body.name is not None:
            row.name = body.name
        if body.archived is not None:
            row.archived = body.archived
        await db.audit(session, identity.user.id, "prompt.update", row.id)
        return record(row)


@router.post("/templates/{template_id}/versions", status_code=201)
async def append_version(template_id: str, body: VersionBody, identity: Identity = Depends(admin)):
    async with db.transaction() as session:
        template = await session.get(db.PromptTemplate, template_id)
        if template is None:
            raise HTTPException(404, "Prompt template not found")
        if template.archived:
            raise HTTPException(409, "Archived templates cannot receive versions")
        latest = await session.scalar(select(func.max(db.PromptVersion.version)).where(db.PromptVersion.template_id == template_id))
        row = db.PromptVersion(template_id=template_id, version=latest + 1, content=body.content,
                               variables=variables(body.content), created_by=identity.user.id)
        session.add(row)
        await session.flush()
        await db.audit(session, identity.user.id, "prompt.version", template_id, version=row.version)
        return record(row)


@router.get("/templates/{template_id}/versions")
async def versions(template_id: str, identity: Identity = Depends(admin), limit: int = Query(100, ge=1, le=100),
                   offset: int = Query(0, ge=0)):
    async with db.session_factory() as session:
        rows = await session.scalars(select(db.PromptVersion).where(db.PromptVersion.template_id == template_id)
                                     .order_by(db.PromptVersion.version).offset(offset).limit(limit))
        return [record(row) for row in rows]


@router.get("/templates/{template_id}/versions/{number}")
async def version(template_id: str, number: int, identity: Identity = Depends(admin)):
    async with db.session_factory() as session:
        return record(await get_version(session, template_id, number))


@router.post("/templates/{template_id}/versions/{number}/render")
async def render(template_id: str, number: int, body: RenderBody, identity: Identity = Depends(admin)):
    async with db.session_factory() as session:
        row = await get_version(session, template_id, number)
        if set(row.variables) != set(body.variables):
            raise HTTPException(400, "Variables must exactly match version's declared placeholders")
        size = len(row.content) + sum(len(body.variables[m.group(1)]) - len(m.group(0)) for m in VARIABLE.finditer(row.content))
        if size > MAX_RENDERED:
            raise HTTPException(400, "Rendered prompt exceeds 131072 characters")
        # Callback replacement is literal: no eval, attributes, filters, escaping,
        # backreference interpolation, filesystem access or recursive expansion.
        content = VARIABLE.sub(lambda match: body.variables[match.group(1)], row.content)
        return {"prompt_version_id": row.id, "content": content}


@router.post("/templates/{template_id}/versions/{number}/evaluations", status_code=201)
async def run(template_id: str, number: int, body: RunBody, identity: Identity = Depends(admin)):
    try:
        report = evaluate([case.model_dump() for case in body.cases], body.rules)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    async with db.transaction() as session:
        version = await get_version(session, template_id, number)
        row = db.EvaluationRun(prompt_version_id=version.id, rules=body.rules, report=report, created_by=identity.user.id)
        session.add(row)
        await session.flush()
        await db.audit(session, identity.user.id, "prompt.evaluate", version.id, run_id=row.id, case_count=len(body.cases))
        return record(row)


@router.get("/evaluations")
async def runs(identity: Identity = Depends(admin), prompt_version_id: str | None = None,
               limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
    query = select(db.EvaluationRun)
    if prompt_version_id is not None:
        query = query.where(db.EvaluationRun.prompt_version_id == prompt_version_id)
    async with db.session_factory() as session:
        rows = await session.scalars(query.order_by(db.EvaluationRun.id).limit(limit).offset(offset))
        return [record(row) for row in rows]


@router.get("/evaluations/{run_id}")
async def get_run(run_id: str, identity: Identity = Depends(admin)):
    async with db.session_factory() as session:
        row = await session.get(db.EvaluationRun, run_id)
        if row is None:
            raise HTTPException(404, "Evaluation run not found")
        return record(row)
