import json
from functools import lru_cache
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")
    database_url: str = "sqlite+aiosqlite:///./gateway.db"
    gateway_master_key: str
    gateway_signing_key: str = Field(min_length=32)
    bootstrap_token: str = Field(min_length=32)
    environment: str = "development"
    public_url: str = "http://localhost:8080"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_group_mapping: str = "{}"
    oidc_group_claim: str = "groups"
    oidc_session_seconds: int = Field(default=28800, ge=300, le=86400)
    oidc_cache_seconds: int = Field(default=300, ge=10, le=3600)
    oidc_trusted_endpoint_origins: str = "[]"
    metrics_token: str = ""
    provider_timeout_seconds: int = Field(default=120, ge=5, le=600)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    max_output_tokens: int = Field(default=4096, ge=1, le=32768)
    allow_private_provider_hosts: bool = False

    @model_validator(mode="after")
    def validate_secrets(self):
        Fernet(self.gateway_master_key.encode())
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
    def groups(self):
        return json.loads(self.oidc_group_mapping)


@lru_cache
def get_settings():
    return Settings()
