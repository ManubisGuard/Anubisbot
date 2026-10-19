from decimal import Decimal
import unittest

from manubisguard.resellers.billing import (
    BYTES_PER_GIB,
    calculate_hourly_accrual,
    calculate_usage_accrual,
)


class HourlyAccrualTests(unittest.TestCase):
    def test_partial_minutes_do_not_advance_cursor(self):
        result = calculate_hourly_accrual(
            hourly_rate=Decimal("120"), last_billed_at=1000, now=1059
        )
        self.assertEqual(result.charge_amount, Decimal("0"))
        self.assertEqual(result.next_billed_at, 1000)
        self.assertEqual(result.billable_minutes, 0)

    def test_fractional_amount_is_carried_between_runs(self):
        first = calculate_hourly_accrual(
            hourly_rate=Decimal("45"), last_billed_at=0, now=1800
        )
        self.assertEqual(first.charge_amount, Decimal("22"))
        self.assertEqual(first.fractional_carry, Decimal("0.5"))
        self.assertEqual(first.next_billed_at, 1800)
        second = calculate_hourly_accrual(
            hourly_rate=Decimal("45"), last_billed_at=first.next_billed_at,
            now=3600, fractional_carry=first.fractional_carry,
        )
        self.assertEqual(second.charge_amount, Decimal("23"))
        self.assertEqual(second.fractional_carry, Decimal("0.0"))

    def test_long_gap_bills_all_complete_minutes(self):
        result = calculate_hourly_accrual(
            hourly_rate=Decimal("60"), last_billed_at=100, now=100 + 60 * 125 + 30
        )
        self.assertEqual(result.charge_amount, Decimal("125"))
        self.assertEqual(result.billable_minutes, 125)
        self.assertEqual(result.next_billed_at, 100 + 60 * 125)

    def test_rejects_negative_time_and_bad_rate(self):
        with self.assertRaises(ValueError):
            calculate_hourly_accrual(hourly_rate=Decimal("1"), last_billed_at=10, now=9)
        with self.assertRaises(ValueError):
            calculate_hourly_accrual(hourly_rate=Decimal("0"), last_billed_at=0, now=60)

    def test_rejects_invalid_fractional_carry(self):
        with self.assertRaises(ValueError):
            calculate_hourly_accrual(
                hourly_rate=Decimal("60"), last_billed_at=0, now=60,
                fractional_carry=Decimal("1"),
            )


class UsageAccrualTests(unittest.TestCase):
    def test_gib_usage_charge_and_fractional_carry(self):
        first = calculate_usage_accrual(
            delta_bytes=BYTES_PER_GIB // 2, rate_per_gib=Decimal("5")
        )
        self.assertEqual(first.charge_amount, Decimal("2"))
        self.assertEqual(first.fractional_carry, Decimal("0.5"))
        second = calculate_usage_accrual(
            delta_bytes=BYTES_PER_GIB // 2, rate_per_gib=Decimal("5"),
            fractional_carry=first.fractional_carry,
        )
        self.assertEqual(second.charge_amount, Decimal("3"))
        self.assertEqual(second.fractional_carry, Decimal("0.0"))

    def test_zero_usage_preserves_carry(self):
        result = calculate_usage_accrual(
            delta_bytes=0, rate_per_gib=Decimal("8"), fractional_carry=Decimal("0.75")
        )
        self.assertEqual(result.charge_amount, Decimal("0"))
        self.assertEqual(result.fractional_carry, Decimal("0.75"))

    def test_rejects_negative_usage_or_rate(self):
        with self.assertRaises(ValueError):
            calculate_usage_accrual(delta_bytes=-1, rate_per_gib=Decimal("1"))
        with self.assertRaises(ValueError):
            calculate_usage_accrual(delta_bytes=1, rate_per_gib=Decimal("-1"))


if __name__ == "__main__":
    unittest.main()
