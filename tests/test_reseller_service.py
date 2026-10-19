import unittest
from decimal import Decimal
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.service import ProviderReseller, PurchaseError, PurchaseResult, ResellerPurchaseService

class Repo:
    def __init__(self):
        self.balance = Decimal("500")
        self.done = {}
        self.reserved = {}
    def get_completed_purchase(self, *, owner_id, idempotency_key):
        return self.done.get((owner_id, idempotency_key))
    def reserve_purchase(self, *, owner_id, idempotency_key, amount, currency, plan, username, volume_gb):
        self.balance -= amount
        self.reserved["p1"] = amount
        return "p1"
    def finalize_purchase(self, *, purchase_id, account):
        self.done[("owner", "key")] = PurchaseResult(account, self.reserved[purchase_id], "IRR", "key")
    def refund_and_fail(self, *, purchase_id, reason_code):
        self.balance += self.reserved[purchase_id]
    def flag_reconciliation_required(self, *, purchase_id, reason_code):
        self.reconciliation = (purchase_id, reason_code)

class Provider:
    async def username_exists(self, *, panel_id, username):
        return False
    async def create_reseller(self, *, panel_id, username, plan, owner_id, volume_gb):
        return ProviderReseller("provider-1", username, "OneTimePass123!")
    async def delete_reseller(self, *, panel_id, provider_account_id):
        pass

class PurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_purchase_creates_account_and_charges_once(self):
        repo = Repo()
        plan = ResellerPlan("plan", "panel", PricingMode.FIXED, Decimal("100"), provider_role_id=1)
        result = await ResellerPurchaseService(repo, Provider()).purchase(
            owner_id="owner", plan=plan, username="reseller", idempotency_key="key"
        )
        self.assertEqual(result.charged_amount, Decimal("100"))
        self.assertEqual(repo.balance, Decimal("400"))
        self.assertEqual(result.account.provider_account_id, "provider-1")
        self.assertEqual(result.initial_password, "OneTimePass123!")

    async def test_provider_preflight_failure_happens_before_wallet_reservation(self):
        class TrackingRepo(Repo):
            def __init__(self):
                super().__init__()
                self.reserve_calls = 0
            def reserve_purchase(self, **kwargs):
                self.reserve_calls += 1
                return super().reserve_purchase(**kwargs)

        class RejectingProvider(Provider):
            async def validate_plan(self, *, panel_id, username, plan):
                raise RuntimeError("provider role preflight failed")

        repo = TrackingRepo()
        plan = ResellerPlan("plan", "panel", PricingMode.FIXED, Decimal("100"), provider_role_id=1)
        with self.assertRaisesRegex(PurchaseError, "no wallet reservation was made"):
            await ResellerPurchaseService(repo, RejectingProvider()).purchase(
                owner_id="owner", plan=plan, username="reseller", idempotency_key="key"
            )
        self.assertEqual(repo.reserve_calls, 0)
        self.assertEqual(repo.balance, Decimal("500"))

if __name__ == "__main__":
    unittest.main()
