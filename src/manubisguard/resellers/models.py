"""Provider-independent reseller domain types.

These are clean domain models, not SQLAlchemy persistence models and not provider API payloads.
Persisted models and provider provisioning are intentionally separate layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class PricingMode(StrEnum):
    FIXED = "fixed"
    UNLIMITED = "unlimited"
    PER_GB = "per_gb"
    PER_TB = "per_tb"
    HOURLY = "hourly"
    USAGE = "usage"


class ResellerStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED_BY_OWNER = "paused_by_owner"
    PAUSED_BY_ADMIN = "paused_by_admin"
    EXPIRED = "expired"
    SUSPENDED_BALANCE = "suspended_balance"
    RECONCILE_REQUIRED = "reconcile_required"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class ResellerPlan:
    """A purchasable provider-reseller plan with explicit limits and pricing."""

    plan_id: str
    panel_id: str
    pricing_mode: PricingMode
    price: Decimal
    unit_price: Decimal = Decimal("0")
    min_volume_gb: Decimal = Decimal("0")
    max_volume_gb: Decimal = Decimal("0")
    volume_step_gb: Decimal = Decimal("1")
    data_limit_bytes: int = 0
    max_users: int = 0
    duration_days: int = 0
    duration_hours: int | None = None
    provider_role_id: int | None = None
    allowed_group_ids: tuple[str, ...] = ()
    permission_overrides: tuple[tuple[str, bool], ...] = ()
    enabled: bool = True
    addon_day_price: Decimal = Decimal("0")
    addon_gb_price: Decimal = Decimal("0")
    addon_user_price: Decimal = Decimal("0")
    currency: str = "IRR"

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise ValueError("plan_id cannot be empty")
        if not self.panel_id.strip():
            raise ValueError("panel_id cannot be empty")
        if self.price < 0 or self.unit_price < 0:
            raise ValueError("prices cannot be negative")
        if self.duration_days < 0 or self.data_limit_bytes < 0 or self.max_users < 0:
            raise ValueError("duration and limits cannot be negative")
        if self.duration_hours is not None and self.duration_hours < 0:
            raise ValueError("duration_hours cannot be negative")
        if self.min_volume_gb < 0 or self.max_volume_gb < 0 or self.volume_step_gb < 0:
            raise ValueError("volume bounds and step cannot be negative")
        if self.max_volume_gb and self.min_volume_gb > self.max_volume_gb:
            raise ValueError("min_volume_gb cannot exceed max_volume_gb")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a three-letter code")


@dataclass(frozen=True, slots=True)
class ResellerAccount:
    """Local reference to a provider-side reseller account; secrets are never stored here."""

    account_id: str
    owner_id: str
    panel_id: str
    provider_account_id: str | None
    username: str
    plan_id: str | None
    pricing_mode: PricingMode
    status: ResellerStatus = ResellerStatus.PENDING
    max_users: int | None = None
    traffic_cap_bytes: int | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    unit_price: Decimal = Decimal("0")
    currency: str = "IRR"
    purchased_volume_gb: Decimal = Decimal("0")
    total_billed: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for field_name in ("account_id", "owner_id", "panel_id", "username"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} cannot be empty")
        if self.max_users is not None and self.max_users < 0:
            raise ValueError("max_users cannot be negative")
        if self.traffic_cap_bytes is not None and self.traffic_cap_bytes < 0:
            raise ValueError("traffic_cap_bytes cannot be negative")

    def is_owned_by(self, actor_id: str) -> bool:
        """Simple domain ownership check; authorization must still be enforced at service/API level."""
        return self.owner_id == actor_id
