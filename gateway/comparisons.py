"""Offline paired evaluation gates without provider calls or retained outputs."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from . import db
from .evaluation import evaluate
from .schemas import Strict
from .security import Identity, admin, revalidate_admin

router = APIRouter(prefix="/admin/api/evaluation-comparisons", tags=["evaluation"])


class Pair(Strict):
    expected: str | None = Field(default=None, max_length=65536)
    baseline: str = Field(max_length=65536)
    candidate: str = Field(max_length=65536)


class Comparison(Strict):
    cases: list[Pair] = Field(min_length=1, max_length=100)
    rules: list[str] = Field(default_factory=lambda: ["exact_match"], min_length=1, max_length=4)
    minimum_pass_rate: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    maximum_regressions: int = Field(default=0, ge=0, le=100)


@router.post("")
async def compare(body: Comparison, identity: Identity = Depends(admin)):
    # Every pair shares the exact expected value; no unverified saved dataset IDs.
    try:
        reports = {name: evaluate([{"expected": case.expected, "output": getattr(case, name)}
                                   for case in body.cases], body.rules)
                   for name in ("baseline", "candidate")}
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    old, new = reports["baseline"], reports["candidate"]
    regressions, improvements = [], []
    for before, after in zip(old["results"], new["results"], strict=True):
        if before["passed"] and not after["passed"]:
            regressions.append(after["index"])
        if after["passed"] and not before["passed"]:
            improvements.append(after["index"])
    accepted = new["pass_rate"] >= body.minimum_pass_rate and len(regressions) <= body.maximum_regressions
    async with db.transaction() as session:
        await revalidate_admin(session, identity)
        await db.audit(session, identity.user.id, "evaluation.compare", cases=len(body.cases),
                       rules=body.rules, accepted=accepted, regressions=len(regressions))
    return {**reports, "regression_indices": regressions, "improvement_indices": improvements,
            "pass_rate_delta": new["pass_rate"] - old["pass_rate"],
            "gate": {"accepted": accepted, "minimum_pass_rate": body.minimum_pass_rate,
                     "maximum_regressions": body.maximum_regressions},
            "scope": "Offline deterministic paired checks; not model quality or safety certification"}
