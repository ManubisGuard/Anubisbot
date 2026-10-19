"""Pure billing arithmetic for hourly and usage-based reseller plans.

This module intentionally does not mutate wallets, provider accounts, billing cursors or ledgers.
The persistence layer must atomically commit the returned charge, ledger entry and new cursor.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

BYTES_PER_GIB = 1024**3
MIN_BILLING_INTERVAL_SECONDS = 60
ZERO = Decimal("0")
ONE = Decimal("1")
SIXTY = Decimal("60")


@dataclass(frozen=True, slots=True)
class BillingAccrual:
    """One deterministic billing calculation; it is not a persisted ledger entry."""

    charge_amount: Decimal
    next_billed_at: int | None
    fractional_carry: Decimal
    billable_minutes: int = 0


def _validate_carry(carry: Decimal) -> None:
    if not carry.is_finite() or carry < ZERO or carry >= ONE:
        raise ValueError("fractional_carry must be finite and in [0, 1)")


def calculate_hourly_accrual(
    *,
    hourly_rate: Decimal,
    last_billed_at: int,
    now: int,
    fractional_carry: Decimal = ZERO,
) -> BillingAccrual:
    """Calculate the charge for complete elapsed minutes at a per-hour rate.

    Fractional currency units are carried forward rather than lost at every run. The cursor only
    advances by complete minutes; callers must persist this result atomically with the wallet debit
    and immutable ledger row, using a per-account lock/idempotency mechanism.
    """
    rate = Decimal(str(hourly_rate))
    carry = Decimal(str(fractional_carry))
    _validate_carry(carry)
    if not rate.is_finite() or rate <= ZERO:
        raise ValueError("hourly_rate must be finite and greater than zero")
    if last_billed_at < 0 or now < 0:
        raise ValueError("timestamps must be non-negative Unix timestamps")
    if now < last_billed_at:
        raise ValueError("now cannot be earlier than last_billed_at")

    minutes = (now - last_billed_at) // MIN_BILLING_INTERVAL_SECONDS
    if minutes <= 0:
        return BillingAccrual(ZERO, last_billed_at, carry, 0)

    exact = rate * Decimal(minutes) / SIXTY + carry
    charge = exact.to_integral_value(rounding=ROUND_DOWN)
    remainder = exact - charge
    next_billed_at = last_billed_at + minutes * MIN_BILLING_INTERVAL_SECONDS
    return BillingAccrual(charge, next_billed_at, remainder, int(minutes))


def calculate_usage_accrual(
    *,
    delta_bytes: int,
    rate_per_gib: Decimal,
    fractional_carry: Decimal = ZERO,
) -> BillingAccrual:
    """Calculate a usage charge for a byte delta using binary GiB (1024**3 bytes).

    `next_billed_at` is `None` and `billable_minutes` is zero because traffic accrual is counter-based;
    the caller advances the traffic baseline only in the same transaction as the debit and ledger.
    """
    rate = Decimal(str(rate_per_gib))
    carry = Decimal(str(fractional_carry))
    _validate_carry(carry)
    if delta_bytes < 0:
        raise ValueError("delta_bytes cannot be negative")
    if not rate.is_finite() or rate < ZERO:
        raise ValueError("rate_per_gib must be finite and non-negative")

    exact = Decimal(delta_bytes) * rate / Decimal(BYTES_PER_GIB) + carry
    charge = exact.to_integral_value(rounding=ROUND_DOWN)
    return BillingAccrual(charge, None, exact - charge, 0)
