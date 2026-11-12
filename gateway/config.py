import json
from functools import lru_cache
from urllib.parse import urlparse
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", hide_input_in_errors=True)
    database_url: str = "sqlite+aiosqlite:///./gateway.db"
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_timeout_seconds: float = Field(default=30, ge=0.1, le=120)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    db_statement_timeout_ms: int = Field(default=30000, ge=100, le=600000)
    db_lock_timeout_ms: int = Field(default=5000, ge=100, le=600000)
    db_migration_statement_timeout_ms: int = Field(default=0, ge=0, le=86400000)
    db_migration_lock_timeout_ms: int = Field(default=0, ge=0, le=86400000)
    gateway_master_key: str
    gateway_signing_key: str = Field(min_length=32)
    bootstrap_token: str = Field(min_length=32)
    environment: Literal["development", "production"] = "development"
    public_url: str = "http://localhost:8080"
    local_password_enabled: bool = False
    local_session_seconds: int = Field(default=3600, ge=300, le=28800)
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_group_mapping: str = "{}"
    oidc_group_claim: str = "groups"
    oidc_session_seconds: int = Field(default=28800, ge=300, le=86400)
    oidc_cache_seconds: int = Field(default=300, ge=10, le=3600)
    oidc_trusted_endpoint_origins: str = "[]"
    guardrails_output_enabled: bool = False
    guardrails_output_action: Literal["block", "redact"] = "block"
    guardrails_enabled: bool = False
    guardrails_action: Literal["block", "redact"] = "block"
    guardrails_detectors: str = '["secrets", "private_key"]'
    guardrails_max_characters: int = Field(default=262144, ge=1024, le=1048576)
    enrollment_enabled: bool = False
    enrollment_allowed_models: str = "[]"
    enrollment_key_days: int = Field(default=30, ge=1, le=90)
    enrollment_rpm: int = Field(default=60, ge=1, le=10000)
    enrollment_tpm: int = Field(default=100000, ge=1, le=10000000)
    enrollment_max_pending: int = Field(default=1000, ge=1, le=10000)
    metrics_token: str = ""
    provider_timeout_seconds: int = Field(default=120, ge=5, le=600)
    provider_response_duration_seconds: float = Field(default=300, ge=1, le=3600)
    provider_stream_duration_seconds: float = Field(default=300, ge=1, le=3600)
    provider_stream_max_bytes: int = Field(default=16_777_216, ge=1024, le=268_435_456)
    provider_stream_max_events: int = Field(default=100_000, ge=1, le=1_000_000)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    max_output_tokens: int = Field(default=4096, ge=1, le=32768)
    allow_private_provider_hosts: bool = False
    circuit_breaker_enabled: bool = True
    circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    circuit_cooldown_seconds: int = Field(default=30, ge=1, le=3600)
    circuit_probe_seconds: int = Field(default=150, ge=5, le=1800)

    @model_validator(mode="after")
    def validate_secrets(self):
        Fernet(self.gateway_master_key.encode())
        public = urlparse(self.public_url)
        if (public.scheme not in {"http", "https"} or not public.hostname or public.username
                or public.password or public.query or public.fragment):
            raise ValueError("PUBLIC_URL requires an HTTP(S) URL without credentials, query or fragment")
        models = json.loads(self.enrollment_allowed_models)
        if (not isinstance(models, list) or len(models) > 100
                or any(not isinstance(m, str) or not m or len(m) > 128 for m in models)
                or len(set(models)) != len(models) or (self.enrollment_enabled and not models)):
            raise ValueError("ENROLLMENT_ALLOWED_MODELS must be unique model aliases, nonempty when enabled")
        from .guardrails import BUILTINS
        detectors = json.loads(self.guardrails_detectors)
        if not isinstance(detectors, list) or not detectors or len(detectors) > len(BUILTINS) or any(not isinstance(x, str) or x not in BUILTINS for x in detectors) or len(set(detectors)) != len(detectors):
            raise ValueError("GUARDRAILS_DETECTORS must contain unique builtin detector IDs")
        if self.db_lock_timeout_ms > self.db_statement_timeout_ms:
            raise ValueError("DB_LOCK_TIMEOUT_MS must not exceed DB_STATEMENT_TIMEOUT_MS")
        if not self.oidc_group_claim or len(self.oidc_group_claim) > 256 or any(not part for part in self.oidc_group_claim.split(".")):
            raise ValueError("OIDC_GROUP_CLAIM must be a nonempty dot-separated object path")
        origins = json.loads(self.oidc_trusted_endpoint_origins)
        if not isinstance(origins, list) or not all(isinstance(x, str) for x in origins):
            raise ValueError("OIDC_TRUSTED_ENDPOINT_ORIGINS must be a JSON string array")
        for origin in origins:
            parsed = urlparse(origin)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("Trusted OIDC endpoint origins must be exact HTTPS origins without paths")
        mapping = json.loads(self.oidc_group_mapping)
        if not isinstance(mapping, dict):
            raise ValueError("OIDC_GROUP_MAPPING must be an object")
        for group, entry in mapping.items():
            if not isinstance(entry, dict) or entry.get("role") not in {"admin", "member"}:
                raise ValueError(f"Invalid OIDC group mapping: {group}")
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+asyncpg://"):
                raise ValueError("Production requires external PostgreSQL")
            if urlparse(self.public_url).scheme != "https":
                raise ValueError("Production PUBLIC_URL requires HTTPS")
            if len(self.metrics_token) < 32:
                raise ValueError("Production METRICS_TOKEN requires at least 32 characters")
        return self

    @property
    def effective_circuit_probe_seconds(self):
        # Cover both per-read timeout and the absolute streaming deadline.
        return max(self.circuit_probe_seconds, self.provider_timeout_seconds + 30,
                   self.provider_stream_duration_seconds + 30, self.provider_response_duration_seconds + 30)

    @property
    def groups(self):
        return json.loads(self.oidc_group_mapping)


@lru_cache
def get_settings():
    return Settings()
