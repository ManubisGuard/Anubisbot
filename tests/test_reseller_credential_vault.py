import os
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from cryptography.fernet import Fernet
from manubisguard.customer_store import CustomerDataStore
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.service import ProviderReseller, ResellerPurchaseService, PurchaseError

class NativeProvider:
    requires_role = True
    requires_persistent_credentials = True
    async def username_exists(self, **kw): return False
    async def create_reseller(self, *, panel_id, username, plan, owner_id, volume_gb):
        return ProviderReseller("admin-1", username, "example-initial-credential")
    async def delete_reseller(self, **kw): pass

class ResellerCredentialVaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustomerDataStore(str(Path(self.tmp.name) / "db.sqlite3"), tenant_id="t")
        self.store.ensure_customer(900)
        self.store.register_panel_connection(panel_id="pg", label="PG", provider="pasarguard",
            base_url="https://pg.example", auth_mode="api_key", secret_ref="env:PG_KEY", enabled=True)
        self.store.post_wallet_entry(telegram_id=900, idempotency_key="fund", amount_minor=1000, entry_type="deposit")
        self.store.put_reseller_plan(plan_id="native", panel_id="pg", title="Native",
            pricing_mode="fixed", price_minor=100, provider_role_id="3")
        self.plan = ResellerPlan(plan_id="native", panel_id="pg", pricing_mode=PricingMode.FIXED,
            price=Decimal(100), provider_role_id=3, currency="IRR")
        self.key = Fernet.generate_key().decode()
    def tearDown(self): self.tmp.cleanup()

    async def test_credentials_are_encrypted_and_recoverable_with_external_key(self):
        with patch.dict(os.environ, {"ANUBIS_CREDENTIAL_ENCRYPTION_KEY": self.key}):
            result = await ResellerPurchaseService(self.store, NativeProvider()).purchase(
                owner_id="900", plan=self.plan, username="native900", idempotency_key="native-buy")
            credential = self.store.get_reseller_credential(result.account.account_id)
            self.assertEqual(credential["password"], "example-initial-credential")
            with self.store._connect() as db:
                raw = db.execute("SELECT encrypted_password FROM reseller_credentials WHERE tenant_id=? AND account_id=?",
                    ("t", result.account.account_id)).fetchone()[0]
            self.assertNotIn("example-initial-credential", raw)
            self.assertNotIn("example-initial-credential", repr(result))
        with patch.dict(os.environ, {"ANUBIS_CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            with self.assertRaisesRegex(ValueError, "cannot be decrypted"):
                self.store.get_reseller_credential(result.account.account_id)

    async def test_missing_vault_key_fails_before_wallet_reservation(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PurchaseError, "vault is not ready"):
                await ResellerPurchaseService(self.store, NativeProvider()).purchase(
                    owner_id="900", plan=self.plan, username="native900", idempotency_key="native-buy-no-key")
        self.assertEqual(self.store.wallet_balance(900), 1000)
        self.assertEqual(self.store.list_all_reseller_accounts(), [])

if __name__ == "__main__": unittest.main()
