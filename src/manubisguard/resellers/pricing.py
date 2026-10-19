"""Deterministic reseller plan pricing and volume validation."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import PricingMode, ResellerPlan

VOLUME_MODES = frozenset({PricingMode.PER_GB, PricingMode.PER_TB})
PREPAID_MODES = frozenset({PricingMode.FIXED, PricingMode.UNLIMITED})


def calculate_purchase_price(plan: ResellerPlan, volume_gb: Decimal | int | str | None = None) -> Decimal:
    """Calculate the initial charge; returns currency units rounded to whole units.

    Hourly/usage plans return the configured starting charge. Actual metered billing is a
    separate ledger operation and must not be inferred from this helper.
    """
    if plan.pricing_mode in PREPAID_MODES:
        amount = plan.price
    elif plan.pricing_mode in VOLUME_MODES:
        if volume_gb is None:
            raise ValueError("volume_gb is required for volume-priced plans")
        volume = Decimal(str(volume_gb))
        valid, reason = validate_volume(plan, volume)
        if not valid:
            raise ValueError(reason)
        multiplier = Decimal("1") if plan.pricing_mode == PricingMode.PER_GB else Decimal("1") / Decimal("1024")
        amount = plan.unit_price * volume * multiplier
    else:
        amount = plan.price
    if amount < 0:
        raise ValueError("calculated amount cannot be negative")
    return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def validate_volume(plan: ResellerPlan, volume_gb: Decimal | int | str) -> tuple[bool, str]:
    """Validate volume input using Decimal arithmetic to avoid float-modulo errors."""
    if plan.pricing_mode not in VOLUME_MODES:
        return True, ""
    volume = Decimal(str(volume_gb))
    if volume <= 0:
        return False, "Volume must be greater than zero."
    if plan.min_volume_gb and volume < plan.min_volume_gb:
        return False, f"Minimum volume is {plan.min_volume_gb} GB."
    if plan.max_volume_gb and volume > plan.max_volume_gb:
        return False, f"Maximum volume is {plan.max_volume_gb} GB."
    step = plan.volume_step_gb
    if step > 0:
        base = plan.min_volume_gb
        steps = (volume - base) / step
        if steps != steps.to_integral_value():
            return False, f"Volume must follow a {step} GB step from {base} GB."
    return True, ""
