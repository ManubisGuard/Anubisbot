"""Provider-independent reseller purchase orchestration with explicit compensation.

This layer is intentionally storage/provider agnostic. Production implementations must make
wallet debit + order reservation atomic in their database, and use an idempotency key for retries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import PricingMode, ResellerAccount, ResellerPlan, ResellerStatus
from .pricing import calculate_purchase_price


class PurchaseError(RuntimeError):
    """Expected, user-safe purchase failure."""


class InsufficientBalance(PurchaseError):
    pass


class DuplicatePurchase(PurchaseError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderReseller:
    provider_account_id: str
    username: str
    # Initial password is returned once for private delivery and is never persisted or logged.
    initial_password: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    account: ResellerAccount
    charged_amount: Decimal
    currency: str
    idempotency_key: str
    initial_password: str = field(default="", repr=False, compare=False)


class PurchaseRepository(Protocol):
    """Persistence contract. reserve_purchase must be atomic and idempotent."""

    def reserve_purchase(
        self, *, owner_id: str, idempotency_key: str, amount: Decimal, currency: str,
        plan: ResellerPlan, username: str, volume_gb: Decimal | None,
    ) -> str: ...

    def finalize_purchase(self, *, purchase_id: str, account: ResellerAccount) -> None: ...

    def refund_and_fail(self, *, purchase_id: str, reason_code: str) -> None: ...

    def flag_reconciliation_required(self, *, purchase_id: str, reason_code: str) -> None: ...

    def get_completed_purchase(self, *, owner_id: str, idempotency_key: str) -> PurchaseResult | None: ...

    def ensure_credential_vault_ready(self) -> None: ...

    def save_reseller_credential(self, *, account_id: str, username: str, password: str) -> None: ...



class ResellerProvider(Protocol):
    async def username_exists(self, *, panel_id: str, username: str) -> bool: ...

    async def create_reseller(
        self, *, panel_id: str, username: str, plan: ResellerPlan, owner_id: str,
        volume_gb: Decimal | None,
    ) -> ProviderReseller: ...

    async def delete_reseller(self, *, panel_id: str, provider_account_id: str) -> None: ...


class ResellerPurchaseService:
    """Coordinates a reseller purchase and compensates a provider account on DB failure."""

    def __init__(self, repository: PurchaseRepository, provider: ResellerProvider):
        self._repository = repository
        self._provider = provider

    async def purchase(
        self, *, owner_id: str, plan: ResellerPlan, username: str,
        idempotency_key: str, volume_gb: Decimal | int | str | None = None,
    ) -> PurchaseResult:
        if not owner_id.strip() or not username.strip() or not idempotency_key.strip():
            raise PurchaseError("Owner, username and idempotency key are required")
        if not plan.enabled:
            raise PurchaseError("Plan is unavailable")
        if getattr(self._provider, "requires_role", True) and not plan.provider_role_id:
            raise PurchaseError("This native reseller plan has no verified provider role ID")
        if getattr(self._provider, "requires_persistent_credentials", False):
            try:
                self._repository.ensure_credential_vault_ready()
            except Exception as exc:
                raise PurchaseError(f"Encrypted credential vault is not ready ({type(exc).__name__}); no wallet reservation was made") from None
        previous = self._repository.get_completed_purchase(
            owner_id=owner_id, idempotency_key=idempotency_key
        )
        if previous is not None:
            return previous
        validate_plan = getattr(self._provider, "validate_plan", None)
        if validate_plan is not None:
            try:
                await validate_plan(panel_id=plan.panel_id, username=username, plan=plan)
            except Exception as exc:
                raise PurchaseError(f"Reseller plan failed provider preflight ({type(exc).__name__}); no wallet reservation was made") from None
        volume = Decimal(str(volume_gb)) if volume_gb is not None else None
        from .pricing import validate_volume
        if plan.pricing_mode in {PricingMode.PER_GB, PricingMode.PER_TB}:
            if volume is None:
                raise PurchaseError("Volume is required for this plan")
            valid, reason = validate_volume(plan, volume)
            if not valid:
                raise PurchaseError(reason)
        amount = calculate_purchase_price(plan, volume)
        purchase_id = self._repository.reserve_purchase(
            owner_id=owner_id, idempotency_key=idempotency_key,
            amount=amount, currency=plan.currency, plan=plan, username=username, volume_gb=volume,
        )
        created: ProviderReseller | None = None
        provider_create_started = False
        try:
            if await self._provider.username_exists(panel_id=plan.panel_id, username=username):
                self._repository.refund_and_fail(purchase_id=purchase_id, reason_code="username_exists")
                raise PurchaseError("Username already exists on this panel")
            provider_create_started = True
            created = await self._provider.create_reseller(
                panel_id=plan.panel_id, username=username, plan=plan,
                owner_id=owner_id, volume_gb=volume,
            )
            if created.initial_password and getattr(self._provider, "requires_persistent_credentials", False):
                self._repository.save_reseller_credential(account_id=purchase_id, username=created.username,
                                                          password=created.initial_password)
            account = ResellerAccount(
                account_id=purchase_id, owner_id=owner_id, panel_id=plan.panel_id,
                provider_account_id=created.provider_account_id, username=username,
                plan_id=plan.plan_id, pricing_mode=plan.pricing_mode,
                status=ResellerStatus.ACTIVE, max_users=plan.max_users or None,
                traffic_cap_bytes=(int(volume * Decimal(1024**3)) if volume is not None and plan.pricing_mode in {PricingMode.PER_GB, PricingMode.PER_TB} else plan.data_limit_bytes) or None,
                created_at=datetime.now(timezone.utc),
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=(plan.duration_hours if plan.duration_hours is not None else plan.duration_days * 24)) if (plan.duration_hours if plan.duration_hours is not None else plan.duration_days * 24) > 0 and plan.pricing_mode in {PricingMode.FIXED, PricingMode.UNLIMITED} else None),
                unit_price=plan.unit_price, currency=plan.currency,
                purchased_volume_gb=volume or Decimal("0"), total_billed=Decimal("0"),
            )
            self._repository.finalize_purchase(purchase_id=purchase_id, account=account)
        except PurchaseError:
            # A duplicate username has already been refunded above. Preserve expected errors as-is.
            raise
        except Exception:
            compensation_failed = False
            if created is not None:
                try:
                    await self._provider.delete_reseller(
                        panel_id=plan.panel_id, provider_account_id=created.provider_account_id
                    )
                except Exception:
                    compensation_failed = True
            if compensation_failed or (provider_create_started and created is None):
                # The provider may have committed before a timeout; never refund and blindly retry.
                self._repository.flag_reconciliation_required(purchase_id=purchase_id,
                    reason_code="provider_cleanup_failed" if compensation_failed else "provider_create_ambiguous")
                raise PurchaseError("Purchase needs operator reconciliation; funds remain reserved") from None
            self._repository.refund_and_fail(purchase_id=purchase_id, reason_code="provisioning_failed")
            raise PurchaseError("Purchase failed; reservation was reversed") from None

        return PurchaseResult(account, amount, plan.currency, idempotency_key, created.initial_password)
