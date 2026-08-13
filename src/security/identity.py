"""Service-principal authentication and role/tenant authorization."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config.settings import Settings

Role = Literal["reader", "researcher", "approver", "admin"]


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    tenant_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    roles: frozenset[Role]


class _PrincipalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: str
    tenant_id: str
    roles: list[Role]
    api_key: str = Field(min_length=16, max_length=512)


class PrincipalRegistry:
    """Parse credentials once and retain only keyed hashes in memory."""

    def __init__(self, settings: Settings) -> None:
        self._entries: list[tuple[bytes, Principal]] = []
        raw = settings.api_principals_json.get_secret_value()
        if raw:
            try:
                configs = [_PrincipalConfig.model_validate(item) for item in json.loads(raw)]
            except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                raise ValueError("API_PRINCIPALS_JSON is invalid") from exc
            for config in configs:
                self._entries.append(
                    (
                        self._digest(config.api_key),
                        Principal(
                            principal_id=config.principal_id,
                            tenant_id=config.tenant_id,
                            roles=frozenset(config.roles),
                        ),
                    )
                )
        elif settings.api_key.get_secret_value():
            self._entries.append(
                (
                    self._digest(settings.api_key.get_secret_value()),
                    Principal(
                        principal_id="legacy-service",
                        tenant_id="default",
                        roles=frozenset({"reader", "researcher", "approver", "admin"}),
                    ),
                )
            )
        if settings.api_auth_required and not self._entries:
            raise ValueError(
                "API_PRINCIPALS_JSON or API_KEY must be configured when API auth is required"
            )

    @staticmethod
    def _digest(api_key: str) -> bytes:
        return hashlib.sha256(api_key.encode("utf-8")).digest()

    def authenticate(self, api_key: str | None) -> Principal | None:
        if not api_key:
            return None
        candidate = self._digest(api_key)
        matched: Principal | None = None
        for digest, principal in self._entries:
            if secrets.compare_digest(candidate, digest):
                matched = principal
        return matched


def has_role(principal: Principal, *roles: Role) -> bool:
    return "admin" in principal.roles or any(role in principal.roles for role in roles)
