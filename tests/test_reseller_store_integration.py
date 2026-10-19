import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from manubisguard.customer_store import CustomerDataStore, CustomerStoreError
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.service import ProviderReseller, ResellerPurchaseService, PurchaseError

class Provider:
    def __init__(self, fail=False): self.fail=fail; self.calls=0
    async def username_exists(self, *, panel_id, username): return False
    async def create_reseller(self, *, panel_id, username, plan, owner_id, volume_gb):
        self.calls+=1
        if self.fail: raise TimeoutError('uncertain provider response')
        return ProviderReseller('pg-admin-7', username, 'OneTimePassword!234')
    async def delete_reseller(self, *, panel_id, provider_account_id): pass

class ResellerStoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=CustomerDataStore(str(Path(self.tmp.name)/'db.sqlite3'),tenant_id='t1')
        self.store.ensure_customer(500)
        self.store.register_panel_connection(panel_id='pg',label='PasarGuard',provider='pasarguard',base_url='https://pg.example',auth_mode='api_key',secret_ref='env:PG_KEY',enabled=True)
        self.store.post_wallet_entry(telegram_id=500,idempotency_key='fund',amount_minor=1000,entry_type='deposit')
        self.store.put_reseller_plan(plan_id='hour',panel_id='pg',title='Hourly',pricing_mode='hourly',price_minor=0,unit_price_minor=600,provider_role_id='4')
        self.plan=ResellerPlan(plan_id='hour',panel_id='pg',pricing_mode=PricingMode.HOURLY,price=Decimal(0),unit_price=Decimal(600),provider_role_id=4,currency='IRR')
    def tearDown(self): self.tmp.cleanup()

    async def test_native_reseller_purchase_and_metered_billing(self):
        provider=Provider(); service=ResellerPurchaseService(self.store,provider)
        result=await service.purchase(owner_id='500',plan=self.plan,username='reseller500',idempotency_key='reseller-buy-1')
        self.assertEqual(result.account.provider_account_id,'pg-admin-7')
        self.assertEqual(result.initial_password,'OneTimePassword!234')
        self.assertEqual(len(self.store.list_reseller_accounts(500)),1)
        account=self.store.list_all_reseller_accounts()[0]
        tick=int(account['last_billed_at'])+120
        bill=self.store.bill_reseller_account(account_id=account['account_id'],now=tick)
        self.assertEqual(bill['charged_minor'],20)
        self.assertEqual(self.store.wallet_balance(500),980)
        replay=await service.purchase(owner_id='500',plan=self.plan,username='different-name',idempotency_key='reseller-buy-1')
        self.assertEqual(replay.account.account_id,result.account.account_id)
        self.assertEqual(replay.initial_password,'')
        self.assertEqual(provider.calls,1)

    async def test_ambiguous_provider_timeout_keeps_reservation_and_marks_reconcile(self):
        self.store.post_wallet_entry(telegram_id=500,idempotency_key='fund2',amount_minor=100,entry_type='deposit')
        provider=Provider(fail=True); service=ResellerPurchaseService(self.store,provider)
        with self.assertRaisesRegex(PurchaseError,'reconciliation'):
            await service.purchase(owner_id='500',plan=self.plan,username='reseller-timeout',idempotency_key='reseller-buy-2')
        account=next(a for a in self.store.list_all_reseller_accounts() if a['username']=='reseller-timeout')
        self.assertEqual(account['status'],'reconcile_required')
        self.assertEqual(self.store.wallet_balance(500),1100)
        with self.assertRaises(CustomerStoreError):
            self.store.reserve_purchase(owner_id='500',idempotency_key='reseller-buy-2',amount=Decimal(0),currency='IRR',plan=self.plan,username='reseller-timeout')

    async def test_insufficient_wallet_moves_reseller_to_suspension_queue(self):
        self.store.post_wallet_entry(telegram_id=500,idempotency_key='spend',amount_minor=-995,entry_type='test_spend',allow_negative=False)
        provider=Provider(); result=await ResellerPurchaseService(self.store,provider).purchase(owner_id='500',plan=self.plan,username='reseller-low',idempotency_key='reseller-buy-low')
        account=self.store.list_all_reseller_accounts()[0]
        tick=int(account['last_billed_at'])+600
        bill=self.store.bill_reseller_account(account_id=account['account_id'],now=tick)
        self.assertTrue(bill['suspend_required'])
        pending=self.store.list_reseller_suspensions_pending()
        self.assertEqual(len(pending),1)
        self.store.confirm_reseller_suspended(account_id=account['account_id'])
        self.assertEqual(self.store.list_reseller_suspensions_pending(),[])

if __name__ == '__main__': unittest.main()
