import tempfile
import unittest
import time
from pathlib import Path
from decimal import Decimal
from manubisguard.customer_store import CustomerDataStore, CustomerStoreError
from manubisguard.panels.contracts import PanelCapabilities, PanelUser, UserCreate, UserUpdate, PanelError
from manubisguard.panels.registry import AdapterRegistry
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.providers import PlatformResellerProvider
from manubisguard.resellers.service import ResellerPurchaseService
from manubisguard.resellers.billing_worker import ResellerBillingWorker
from manubisguard.sales.service import CustomerPurchaseService

class FakePanel:
    def __init__(self,kind,used=0): self.kind=kind; self.used=used; self.created=[]; self.updated=[]
    async def capabilities(self,connection): return PanelCapabilities(create_user=True,subscription_link=True)
    async def health_check(self,connection): return {'ok':True}
    async def create_user(self,connection,user:UserCreate):
        self.created.append(user)
        return PanelUser(provider_user_id=user.username,username=user.username,status='active',subscription_url=f'https://sub.example/{user.username}')
    async def get_subscription_url(self,connection,username): return f'https://sub.example/{username}'
    async def get_usage(self,connection,username): return {'used_bytes':self.used}
    async def update_user(self,connection,provider_user_id,update:UserUpdate): self.updated.append((provider_user_id,update.status))
    async def get_user(self,connection,username): raise NotImplementedError
    async def delete_user(self,connection,provider_user_id): raise NotImplementedError

class PlatformResellerWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=CustomerDataStore(str(Path(self.tmp.name)/'db.sqlite3'),tenant_id='t')
        self.store.ensure_customer(808)
        self.store.register_panel_connection(panel_id='rb',label='Rebecca',provider='rebecca',base_url='https://rb.example',auth_mode='api_key',secret_ref='env:REBECCA_KEY',enabled=True,metadata={'service_id':123})
        self.store.post_wallet_entry(telegram_id=808,idempotency_key='seed',amount_minor=10000,entry_type='deposit')
        self.panel=FakePanel('rebecca'); self.registry=AdapterRegistry(); self.registry.register(self.panel)
        self.provider=PlatformResellerProvider(self.store)
    def tearDown(self): self.tmp.cleanup()

    async def test_sanaei_is_rejected_for_reseller_plans(self):
        self.store.register_panel_connection(panel_id="sx", label="Sanaei", provider="sanaei",
            base_url="https://sx.example", auth_mode="access_token", secret_ref="env:SANAEI_TOKEN",
            enabled=True, metadata={"inbound_ids": [1]})
        plan = ResellerPlan(plan_id="sx-plan", panel_id="sx", pricing_mode=PricingMode.FIXED,
            price=Decimal(100), currency="IRR")
        with self.assertRaisesRegex(PanelError, "Sanaei does not support reseller accounts"):
            await self.provider.validate_plan(panel_id="sx", username="reseller808", plan=plan)

    async def _buy_plan(self, mode=PricingMode.FIXED, setup=1000, rate=0, max_users=1):
        self.store.put_reseller_plan(plan_id='rplan',panel_id='rb',title='Managed reseller',pricing_mode=mode.value,
            price_minor=setup,unit_price_minor=rate,currency='IRR',duration_hours=0,max_users=max_users)
        plan=ResellerPlan(plan_id='rplan',panel_id='rb',pricing_mode=mode,price=Decimal(setup),unit_price=Decimal(rate),
            max_users=max_users,currency='IRR')
        result=await ResellerPurchaseService(self.store,self.provider).purchase(owner_id='808',plan=plan,
            username='reseller808',idempotency_key=f'reseller-{mode.value}')
        self.assertEqual(result.account.provider_account_id,'platform-managed')
        return result

    async def test_platform_reseller_can_create_child_config_without_double_charge(self):
        account=await self._buy_plan()
        self.store.put_product(product_id='cfg1',title='Customer config',price_minor=5000,panel_id='rb',
            duration_hours=24,traffic_bytes=1024**3,pricing_mode='fixed')
        purchase=CustomerPurchaseService(self.store,secret_resolver=lambda _:'panel-token',registry=self.registry)
        result=await purchase.buy_from_wallet(telegram_id=808,product_id='cfg1',reseller_account_id=account.account.account_id,
            idempotency_key='reseller-child-1',now=1000)
        self.assertEqual(result.status,'active')
        self.assertEqual(self.store.wallet_balance(808),9000)  # only the reseller setup fee was charged
        service=self.store.get_customer_service(result.service_id,808)
        self.assertEqual(service['reseller_account_id'],account.account.account_id)
        self.assertEqual(len(self.store.list_reseller_child_services(account.account.account_id)),1)

    async def test_reseller_max_users_is_enforced_atomically(self):
        account=await self._buy_plan(max_users=1)
        self.store.put_product(product_id='cfg1',title='Customer config',price_minor=5000,panel_id='rb',duration_hours=24)
        purchase=CustomerPurchaseService(self.store,secret_resolver=lambda _:'panel-token',registry=self.registry)
        await purchase.buy_from_wallet(telegram_id=808,product_id='cfg1',reseller_account_id=account.account.account_id,
            idempotency_key='child-one',now=1000)
        with self.assertRaisesRegex(CustomerStoreError,'maximum active service count'):
            await purchase.buy_from_wallet(telegram_id=808,product_id='cfg1',reseller_account_id=account.account.account_id,
                idempotency_key='child-two',now=1000)

    async def test_platform_hourly_reseller_suspends_and_resumes_all_child_configs(self):
        account=await self._buy_plan(mode=PricingMode.HOURLY,setup=0,rate=600,max_users=5)
        self.store.put_product(product_id='cfg1',title='Customer config',price_minor=5000,panel_id='rb',duration_hours=24)
        purchase=CustomerPurchaseService(self.store,secret_resolver=lambda _:'panel-token',registry=self.registry)
        result=await purchase.buy_from_wallet(telegram_id=808,product_id='cfg1',reseller_account_id=account.account.account_id,
            idempotency_key='hourly-child',now=int(time.time()))
        self.store.post_wallet_entry(telegram_id=808,idempotency_key='drain-wallet',amount_minor=-10000,entry_type='test_spend')
        worker=ResellerBillingWorker(self.store,secret_resolver=lambda _:'panel-token',provider=self.provider,registry=self.registry)
        acct=self.store.get_reseller_account(account.account.account_id)
        tick=int(acct['last_billed_at'])+600
        result_bill=await worker.run_once(now=tick)
        self.assertEqual(result_bill['suspended'],1)
        self.assertEqual(self.store.get_customer_service(result.service_id,808)['status'],'suspended')
        self.assertIn((result.username,'disabled'),self.panel.updated)
        self.store.post_wallet_entry(telegram_id=808,idempotency_key='restore-wallet',amount_minor=1000,entry_type='deposit')
        await worker.resume(account_id=account.account.account_id,telegram_id=808,now=tick+1)
        self.assertEqual(self.store.get_customer_service(result.service_id,808)['status'],'active')
        self.assertEqual(self.store.get_reseller_account(account.account.account_id)['outstanding_debt_minor'],0)
        self.assertEqual(self.store.wallet_balance(808),900)
        self.assertIn((result.username,'active'),self.panel.updated)

    async def test_platform_usage_plan_bills_aggregate_child_traffic_once(self):
        account=await self._buy_plan(mode=PricingMode.USAGE,setup=0,rate=1024,max_users=5)
        self.store.put_product(product_id='cfg1',title='Customer config',price_minor=5000,panel_id='rb',duration_hours=0,traffic_bytes=0)
        purchase=CustomerPurchaseService(self.store,secret_resolver=lambda _:'panel-token',registry=self.registry)
        result=await purchase.buy_from_wallet(telegram_id=808,product_id='cfg1',reseller_account_id=account.account.account_id,
            idempotency_key='usage-child',now=int(time.time()))
        self.panel.used=1024**3
        worker=ResellerBillingWorker(self.store,secret_resolver=lambda _:'panel-token',provider=self.provider,registry=self.registry)
        tick=int(self.store.list_all_reseller_accounts()[0]['last_billed_at'])+60
        first=await worker.run_once(now=tick)
        second=await worker.run_once(now=tick)
        self.assertEqual(first['billed'],1)
        self.assertEqual(second['billed'],0)
        self.assertEqual(self.store.wallet_balance(808),8976)

if __name__ == '__main__': unittest.main()
