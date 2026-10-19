import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelCapabilities, PanelUser, UserCreate, UserUpdate
from manubisguard.panels.registry import AdapterRegistry
from manubisguard.sales.billing_worker import CustomerBillingWorker
from manubisguard.resellers.billing_worker import ResellerBillingWorker
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.service import ProviderReseller, ResellerPurchaseService

class FakePanel:
    kind='pasarguard'
    def __init__(self,used=0,fail_suspend=False): self.used=used; self.updates=[]; self.fail_suspend=fail_suspend
    async def capabilities(self,connection): return PanelCapabilities(create_user=True)
    async def health_check(self,connection): return {'ok':True}
    async def create_user(self,connection,user): return PanelUser(provider_user_id='provider-1',username=user.username,status='active')
    async def get_usage(self,connection,username): return {'used_bytes':self.used}
    async def update_user(self,connection,provider_user_id,update):
        if self.fail_suspend and update.status=='disabled': raise RuntimeError('transient')
        self.updates.append((provider_user_id,update.status)); return PanelUser(provider_user_id=provider_user_id,username=provider_user_id,status=update.status)
    async def get_user(self,connection,username): return PanelUser(provider_user_id='provider-1',username=username,status='active')
    async def delete_user(self,connection,provider_user_id): return None

class FakeResellerProvider:
    def __init__(self,used=0): self.used=used; self.statuses=[]
    async def username_exists(self,**kw): return False
    async def create_reseller(self,*,panel_id,username,plan,owner_id,volume_gb): return ProviderReseller('admin-1',username,'OneTime!Password123')
    async def delete_reseller(self,**kw): return None
    async def get_reseller(self,**kw): return SimpleNamespace(used_traffic=self.used)
    async def set_reseller_status(self,*,panel_id,provider_account_id,enabled): self.statuses.append(enabled)

class BillingWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=CustomerDataStore(str(Path(self.tmp.name)/'db.sqlite3'),tenant_id='t1')
        self.store.ensure_customer(700)
        self.store.register_panel_connection(panel_id='pg',label='PG',provider='pasarguard',base_url='https://pg.example',auth_mode='api_key',secret_ref='env:PG_KEY',enabled=True)
        self.registry=AdapterRegistry(); self.panel=FakePanel(); self.registry.register(self.panel)
    def tearDown(self): self.tmp.cleanup()

    async def test_customer_hourly_billing_suspends_when_wallet_empty(self):
        self.store.post_wallet_entry(telegram_id=700,idempotency_key='fund',amount_minor=5,entry_type='deposit')
        product={'product_id':'h','panel_id':'pg','pricing_mode':'hourly','unit_price_minor':600,'currency':'IRR','traffic_bytes':0}
        self.store.create_metered_service(service_id='svc',telegram_id=700,product=product,provider_user_id='provider-1',username='alice',subscription_url=None,now=1000)
        worker=CustomerBillingWorker(self.store,secret_resolver=lambda _:'secret',registry=self.registry)
        result=await worker.run_once(now=1600)
        self.assertEqual(result['suspended'],1)
        self.assertEqual(self.store.get_customer_service('svc')['billing_status'],'suspended_balance')
        self.assertEqual(self.panel.updates,[('provider-1','disabled')])

    async def test_customer_can_resume_metered_service_after_wallet_topup(self):
        self.store.post_wallet_entry(telegram_id=700,idempotency_key='resume-fund',amount_minor=5,entry_type='deposit')
        product={'product_id':'h','panel_id':'pg','pricing_mode':'hourly','unit_price_minor':600,'currency':'IRR','traffic_bytes':0}
        self.store.create_metered_service(service_id='svc-resume',telegram_id=700,product=product,provider_user_id='provider-1',username='alice',subscription_url=None,now=1000)
        worker=CustomerBillingWorker(self.store,secret_resolver=lambda _:'secret',registry=self.registry)
        await worker.run_once(now=1600)
        self.assertEqual(self.store.get_customer_service('svc-resume')['outstanding_debt_minor'],95)
        self.store.post_wallet_entry(telegram_id=700,idempotency_key='resume-topup',amount_minor=1000,entry_type='deposit')
        await worker.resume(service_id='svc-resume',telegram_id=700,now=1700)
        self.assertEqual(self.store.get_customer_service('svc-resume')['billing_status'],'active')
        self.assertEqual(self.store.get_customer_service('svc-resume')['outstanding_debt_minor'],0)
        self.assertEqual(self.store.wallet_balance(700),905)
        self.assertEqual(self.panel.updates[-1],('provider-1','active'))

    async def test_customer_usage_billing_uses_provider_counter(self):
        self.store.post_wallet_entry(telegram_id=700,idempotency_key='fund2',amount_minor=5000,entry_type='deposit')
        product={'product_id':'u','panel_id':'pg','pricing_mode':'usage','unit_price_minor':1024,'currency':'IRR','traffic_bytes':0}
        self.store.create_metered_service(service_id='svc-u',telegram_id=700,product=product,provider_user_id='provider-1',username='alice',subscription_url=None,now=1000)
        self.panel.used=1024**3
        worker=CustomerBillingWorker(self.store,secret_resolver=lambda _:'secret',registry=self.registry)
        result=await worker.run_once(now=1060)
        self.assertEqual(result['billed'],1)
        self.assertEqual(self.store.wallet_balance(700),3976)

    async def test_fixed_reseller_expires_and_is_disabled_at_deadline(self):
        self.store.post_wallet_entry(telegram_id=700,idempotency_key='fixed-fund',amount_minor=500,entry_type='deposit')
        self.store.put_reseller_plan(plan_id='fixed',panel_id='pg',title='Fixed',pricing_mode='fixed',price_minor=100,unit_price_minor=0,provider_role_id='3',duration_hours=1)
        plan=ResellerPlan(plan_id='fixed',panel_id='pg',pricing_mode=PricingMode.FIXED,price=Decimal(100),provider_role_id=3,currency='IRR',duration_days=1,duration_hours=1)
        provider=FakeResellerProvider(); await ResellerPurchaseService(self.store,provider).purchase(owner_id='700',plan=plan,username='r700-fixed',idempotency_key='r-fixed')
        account=next(a for a in self.store.list_all_reseller_accounts() if a['username']=='r700-fixed')
        from datetime import datetime
        expires=int(datetime.fromisoformat(account['expires_at']).timestamp())+1
        worker=ResellerBillingWorker(self.store,secret_resolver=lambda _:'secret',provider=provider)
        result=await worker.run_once(now=expires)
        self.assertEqual(result['expired'],1)
        self.assertEqual(provider.statuses,[False])
        updated=next(a for a in self.store.list_all_reseller_accounts() if a['account_id']==account['account_id'])
        self.assertEqual(updated['status'],'expired')

    async def test_reseller_hourly_billing_queues_and_confirms_provider_suspension(self):
        self.store.post_wallet_entry(telegram_id=700,idempotency_key='reseller-fund',amount_minor=5,entry_type='deposit')
        self.store.put_reseller_plan(plan_id='hour',panel_id='pg',title='Hourly',pricing_mode='hourly',price_minor=0,unit_price_minor=600,provider_role_id='3')
        plan=ResellerPlan(plan_id='hour',panel_id='pg',pricing_mode=PricingMode.HOURLY,price=Decimal(0),unit_price=Decimal(600),provider_role_id=3,currency='IRR')
        provider=FakeResellerProvider(); purchase=await ResellerPurchaseService(self.store,provider).purchase(owner_id='700',plan=plan,username='r700',idempotency_key='r-buy')
        account=self.store.list_all_reseller_accounts()[0]
        first_tick=self.store.bill_reseller_account(account_id=account['account_id'],now=int(account['last_billed_at'])+30)
        second_tick=self.store.bill_reseller_account(account_id=account['account_id'],now=int(account['last_billed_at'])+30)
        self.assertTrue(first_tick['skipped'])
        self.assertTrue(second_tick['skipped'])
        worker=ResellerBillingWorker(self.store,secret_resolver=lambda _:'secret',provider=provider)
        result=await worker.run_once(now=int(account['last_billed_at'])+600)
        self.assertEqual(result['suspended'],1)
        self.assertEqual(provider.statuses,[False])
        account_after = self.store.list_all_reseller_accounts()[0]
        self.assertEqual(account_after['status'],'suspended_balance')
        self.assertEqual(account_after['outstanding_debt_minor'],95)

        self.store.post_wallet_entry(telegram_id=700,idempotency_key='reseller-resume-topup',amount_minor=1000,entry_type='deposit')
        await worker.resume(account_id=account_after['account_id'],telegram_id=700,now=int(account_after['last_billed_at'])+1)
        resumed=self.store.get_reseller_account(account_after['account_id'])
        self.assertEqual(resumed['status'],'active')
        self.assertEqual(resumed['outstanding_debt_minor'],0)
        self.assertEqual(self.store.wallet_balance(700),905)
        self.assertEqual(provider.statuses,[False,True])

if __name__ == '__main__': unittest.main()
