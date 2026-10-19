"""Stable, panel-neutral contracts. No provider SDK types cross this boundary."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class PanelError(RuntimeError):
    """Safe, provider-neutral panel operation error."""


@dataclass(frozen=True, slots=True)
class PanelConnection:
    id: str
    tenant_id: str
    name: str
    kind: str
    base_url: str
    auth_mode: str  # api_key | username_password | access_token
    secret_ref: str  # reference to a secret vault; never store raw secrets here
    enabled: bool = True
    verify_tls: bool = True
    timeout_seconds: float = 10.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PanelCapabilities:
    create_user: bool = False
    delete_user: bool = False
    update_user: bool = False
    read_usage: bool = False
    read_status: bool = False
    suspend_user: bool = False
    activate_user: bool = False
    subscription_link: bool = False
    list_users: bool = False
    manage_resellers: bool = False
    health_check: bool = True
    sync: bool = False


@dataclass(frozen=True, slots=True)
class UserCreate:
    username: str
    data_limit_bytes: int | None = None
    expire_at: datetime | None = None
    group_ids: tuple[int, ...] = ()
    note: str | None = None
    protocols: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserUpdate:
    data_limit_bytes: int | None = None
    expire_at: datetime | None = None
    status: str | None = None  # active | disabled | limited | expired
    note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PanelUser:
    provider_user_id: str
    username: str
    status: str | None = None
    used_bytes: int | None = None
    data_limit_bytes: int | None = None
    expire_at: datetime | None = None
    subscription_url: str | None = None
    raw_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PanelAdapter(Protocol):
    kind: str
    async def capabilities(self, connection: PanelConnection) -> PanelCapabilities: ...
    async def health_check(self, connection: PanelConnection) -> dict[str, Any]: ...
    async def create_user(self, connection: PanelConnection, user: UserCreate) -> PanelUser: ...
    async def get_user(self, connection: PanelConnection, username: str) -> PanelUser: ...
    async def update_user(self, connection: PanelConnection, provider_user_id: str, update: UserUpdate) -> PanelUser: ...
    async def delete_user(self, connection: PanelConnection, provider_user_id: str) -> None: ...
