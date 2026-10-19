from decimal import Decimal
import unittest

from manubisguard.resellers import (
    PricingMode,
    ResellerAccount,
    ResellerPlan,
    ResellerStatus,
    calculate_purchase_price,
    validate_volume,
)


class ResellerDomainTests(unittest.TestCase):
    def make_plan(self, **changes):
        values = {
            "plan_id": "plan-a",
            "panel_id": "panel-a",
            "pricing_mode": PricingMode.PER_GB,
            "price": Decimal("0"),
            "unit_price": Decimal("1250"),
            "min_volume_gb": Decimal("5"),
            "max_volume_gb": Decimal("100"),
            "volume_step_gb": Decimal("5"),
            "duration_days": 30,
            "max_users": 10,
        }
        values.update(changes)
        return ResellerPlan(**values)

    def test_volume_pricing_rounds_whole_currency_units(self):
        plan = self.make_plan()
        self.assertEqual(calculate_purchase_price(plan, Decimal("10")), Decimal("12500"))

    def test_volume_required_for_volume_plan(self):
        with self.assertRaisesRegex(ValueError, "volume_gb is required"):
            calculate_purchase_price(self.make_plan())

    def test_volume_step_is_enforced(self):
        valid, reason = validate_volume(self.make_plan(), "12")
        self.assertFalse(valid)
        self.assertIn("step", reason)

    def test_volume_bounds_are_enforced(self):
        valid, _ = validate_volume(self.make_plan(), "105")
        self.assertFalse(valid)

    def test_fixed_price_ignores_volume(self):
        plan = self.make_plan(pricing_mode=PricingMode.FIXED, price=Decimal("30000"))
        self.assertEqual(calculate_purchase_price(plan), Decimal("30000"))

    def test_invalid_plan_bounds_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "min_volume_gb"):
            self.make_plan(min_volume_gb=Decimal("120"), max_volume_gb=Decimal("100"))

    def test_reseller_ownership_and_status_are_explicit(self):
        account = ResellerAccount(
            account_id="acct-1",
            owner_id="tenant-1",
            panel_id="panel-a",
            provider_account_id=None,
            username="reseller_a",
            plan_id="plan-a",
            pricing_mode=PricingMode.FIXED,
            status=ResellerStatus.PENDING,
        )
        self.assertTrue(account.is_owned_by("tenant-1"))
        self.assertFalse(account.is_owned_by("tenant-2"))
        self.assertEqual(account.status, ResellerStatus.PENDING)


if __name__ == "__main__":
    unittest.main()
