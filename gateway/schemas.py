from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TeamCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)


class TeamPatch(Strict):
    budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    enabled: bool | None = None


class UserCreate(Strict):
    email: str = Field(min_length=3, max_length=320)
    team_id: str | None = None
    role: Literal["admin", "member"] = "member"
    budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        if "@" not in value or any(c.isspace() for c in value):
            raise ValueError("Invalid email")
        return value.lower()


class UserPatch(Strict):
    enabled: bool | None = None
    budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    role: Literal["admin", "member"] | None = None


class ModelRoute(Strict):
    upstream_model: str = Field(min_length=1, max_length=128)
    input_cost_per_million: Decimal = Field(ge=0, le=100000, decimal_places=6)
    output_cost_per_million: Decimal = Field(ge=0, le=100000, decimal_places=6)


class ProviderCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["openai", "anthropic"] = "openai"
    base_url: str = Field(max_length=2048)
    api_key: str = Field(min_length=1, max_length=8192)
    models: dict[str, ModelRoute]
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=1000)

    @field_validator("models")
    @classmethod
    def models_valid(cls, value):
        if not 1 <= len(value) <= 100:
            raise ValueError("Between 1 and 100 models required")
        if any(not k or len(k) > 128 for k in value):
            raise ValueError("Invalid model alias")
        return value


class ProviderPatch(Strict):
    enabled: bool | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)
    models: dict[str, ModelRoute] | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)


class KeyCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    user_id: str
    team_id: str | None = None
    expires_in_days: int = Field(default=30, ge=1, le=365)
    kind: Literal["api", "agent"] = "api"
    allowed_models: list[str] = Field(default_factory=list, max_length=100)
    rpm: int = Field(default=60, ge=1, le=100000)
    tpm: int = Field(default=100000, ge=1, le=100000000)


class Bootstrap(Strict):
    email: str = Field(min_length=3, max_length=320)
    token: str


class EvaluationRequest(Strict):
    cases: list[dict] = Field(min_length=1, max_length=100)
    rules: list[str] = Field(default_factory=lambda: ["exact_match"], max_length=10)
