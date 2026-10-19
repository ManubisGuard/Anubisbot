import tempfile
import unittest
from pathlib import Path
from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelCapabilities, PanelUser, UserCreate, UserUpdate
from manubisguard.panels.registry import AdapterRegistry
from manubisguard.sales.service import CustomerPurchaseService, PurchaseNeedsReconciliation

class FakeAdapter:
    kind='pasarguard'
    def __init__(self, fail=False): self.calls=0; self.fail=fail; self.last_user=None
    async def capabilities(self, connection): return PanelCapabilities(create_user=True)
    async def health_check(self, connection): return {"ok": True}
    async def create_user(self, connection, user: UserCreate):
        self.calls+=1; self.last_user=user
        if self.fail: raise TimeoutError('simulated uncertain result')
        return PanelUser(provider_user_id='provider-id',username=user.username,status='active',subscription_url='https://sub.example/abc')
    async def get_user(self,connection,username): raise NotImplementedError
    async def update_user(self,connection,provider_user_id,update:UserUpdate): raise NotImplementedError
    async def delete_user(self,connection,provider_user_id): raise NotImplementedError

class CustomerPurchaseServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=CustomerDataStore(str(Path(self.tmp.name)/'db.sqlite3'),tenant_id='t1')
        self.store.ensure_customer(123)
        self.store.register_panel_connection(panel_id='p1',label='Fake',provider='pasarguard',base_url='https://panel.example',auth_mode='api_key',secret_ref='env:PANEL_KEY',enabled=True)
        self.store.put_product(product_id='basic',title='Basic',price_minor=2500,panel_id='p1',traffic_bytes=1024**3,duration_hours=24,group_ids=['1'])
        self.store.post_wallet_entry(telegram_id=123,idempotency_key='deposit',amount_minor=5000,entry_type='deposit')
        self.adapter=FakeAdapter(); self.registry=AdapterRegistry(); self.registry.register(self.adapter)
        self.sanaei_adapter=FakeAdapter(); self.sanaei_adapter.kind='sanaei'; self.registry.register(self.sanaei_adapter)
        self.service=CustomerPurchaseService(self.store,secret_resolver=lambda _: 'secret',registry=self.registry)
    def tearDown(self): self.tmp.cleanup()

    async def test_wallet_purchase_provisions_and_replay_does_not_charge_twice(self):
        first=await self.service.buy_from_wallet(telegram_id=123,product_id='basic',idempotency_key='buy-1',now=1000)
        second=await self.service.buy_from_wallet(telegram_id=123,product_id='basic',idempotency_key='buy-1',now=1000)
        self.assertEqual(first.order_id,second.order_id); self.assertTrue(second.idempotent_replay)
        self.assertEqual(self.adapter.calls,1); self.assertEqual(self.store.wallet_balance(123),2500)
        service=self.store.get_customer_service(first.service_id,123)
        self.assertEqual(service['subscription_url'],'https://sub.example/abc')
        self.assertEqual(self.adapter.last_user.group_ids,(1,))

    async def test_hourly_product_is_created_without_provider_expiry_or_data_cap(self):
        self.store.put_product(product_id='hour',title='Hourly',price_minor=0,pricing_mode='hourly',unit_price_minor=60,panel_id='p1',duration_hours=0,traffic_bytes=0,group_ids=['1'])
        result=await self.service.buy_from_wallet(telegram_id=123,product_id='hour',idempotency_key='buy-hour',now=1000)
        self.assertEqual(result.status,'active')
        self.assertIsNone(self.adapter.last_user.expire_at)
        self.assertIsNone(self.adapter.last_user.data_limit_bytes)

    async def test_missing_panel_secret_fails_before_wallet_debit(self):
        service=CustomerPurchaseService(self.store,secret_resolver=lambda _: '',registry=self.registry)
        with self.assertRaisesRegex(Exception,'credential is not configured'):
            await service.buy_from_wallet(telegram_id=123,product_id='basic',idempotency_key='buy-no-secret',now=1000)
        self.assertEqual(self.store.wallet_balance(123),5000)

    async def test_rebecca_missing_service_id_is_refunded_before_provider_mutation(self):
        self.store.register_panel_connection(panel_id='rb',label='Rebecca',provider='rebecca',base_url='https://rb.example',auth_mode='api_key',secret_ref='env:RB_KEY',enabled=True)
        self.store.put_product(product_id='rb-no-service',title='Rebecca product',price_minor=1000,panel_id='rb',group_ids=[])
        rebecca=FakeAdapter(); rebecca.kind='rebecca'; self.registry.register(rebecca)
        service=CustomerPurchaseService(self.store,secret_resolver=lambda _:'secret',registry=self.registry)
        with self.assertRaisesRegex(Exception,'reservation was reversed'):
            await service.buy_from_wallet(telegram_id=123,product_id='rb-no-service',idempotency_key='rb-no-service',now=1000)
        self.assertEqual(self.store.wallet_balance(123),5000)
        self.assertEqual(rebecca.calls,0)

    async def test_native_rebecca_reseller_child_purchase_fails_closed_without_scoped_api_key(self):
        self.store.register_panel_connection(panel_id='rb-native',label='Rebecca',provider='rebecca',
            base_url='https://rb.example',auth_mode='api_key',secret_ref='env:RB_KEY',enabled=True,
            metadata={'service_id':12})
        self.store.put_product(product_id='rb-child',title='Rebecca child',price_minor=1000,panel_id='rb-native',group_ids=['12'])
        self.store.put_reseller_plan(plan_id='rb-plan',panel_id='rb-native',title='Native reseller',
            pricing_mode='fixed',price_minor=1000,unit_price_minor=0,currency='IRR',duration_hours=0,max_users=5)
        with self.store._connect() as db:
            db.execute("""INSERT INTO reseller_accounts(tenant_id,account_id,owner_telegram_id,panel_id,
                provider_account_id,username,plan_id,pricing_mode,status,currency)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", ('t1','rb-native-account',123,'rb-native','reseller123',
                'reseller123','rb-plan','fixed','active','IRR'))
        rebecca=FakeAdapter(); rebecca.kind='rebecca'; self.registry.register(rebecca)
        with self.assertRaisesRegex(Exception,'scoped per-admin API key'):
            await self.service.buy_from_wallet(telegram_id=123,product_id='rb-child',
                reseller_account_id='rb-native-account',idempotency_key='rb-child-no-scope',now=1000)
        self.assertEqual(self.store.wallet_balance(123),5000)
        self.assertEqual(rebecca.calls,0)

    async def test_invalid_panel_group_configuration_is_refunded_before_provider_mutation(self):
        self.store.register_panel_connection(panel_id='s1',label='Sanaei',provider='sanaei',base_url='https://sanaei.example',auth_mode='access_token',secret_ref='env:SANAEI_TOKEN',enabled=True)
        self.store.put_product(product_id='bad-group',title='Bad',price_minor=1000,panel_id='s1',group_ids=['not-an-inbound-id'])
        service=CustomerPurchaseService(self.store,secret_resolver=lambda _:'secret',registry=self.registry)
        with self.assertRaisesRegex(Exception,'reservation was reversed'):
            await service.buy_from_wallet(telegram_id=123,product_id='bad-group',idempotency_key='buy-bad-group',now=1000)
        self.assertEqual(self.store.wallet_balance(123),5000)
        self.assertEqual(self.adapter.calls,0)
        self.assertEqual(self.sanaei_adapter.calls,0)

    async def test_ambiguous_provider_failure_is_not_refunded_or_retried(self):
        self.adapter.fail=True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.buy_from_wallet(telegram_id=123,product_id='basic',idempotency_key='buy-uncertain',now=1000)
        balance=self.store.wallet_balance(123)
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.buy_from_wallet(telegram_id=123,product_id='basic',idempotency_key='buy-uncertain',now=1000)
        self.assertEqual(self.adapter.calls,1)
        self.assertEqual(self.store.wallet_balance(123),balance)

    async def test_discount_claim_and_wallet_debit_are_atomic_and_price_is_snapshotted(self):
        self.store.put_discount_code(code='save50', percentage=50, max_uses=1, allowed_product_ids=['basic'])
        result=await self.service.buy_from_wallet(telegram_id=123,product_id='basic',
            idempotency_key='buy-discount',discount_code='save50',now=1000)
        self.assertEqual(result.amount_minor,1250)
        self.assertEqual(self.store.wallet_balance(123),3750)
        order=self.store.get_order_by_idempotency('buy-discount',123)
        self.assertEqual(order['discount_code'],'SAVE50')
        self.assertEqual(order['discount_amount_minor'],1250)
        self.assertEqual(self.store.get_discount_code('save50')['used_count'],1)
        replay=await self.service.buy_from_wallet(telegram_id=123,product_id='basic',
            idempotency_key='buy-discount',discount_code='SAVE50',now=1000)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(self.adapter.calls,1)
        with self.assertRaisesRegex(Exception,'usage limit'):
            await self.service.buy_from_wallet(telegram_id=123,product_id='basic',
                idempotency_key='buy-discount-second',discount_code='SAVE50',now=1000)
        self.assertEqual(self.store.wallet_balance(123),3750)

    async def test_expired_discount_is_rejected_without_wallet_debit_or_claim(self):
        self.store.put_discount_code(code='OLD',percentage=20,max_uses=1,expires_at=999)
        with self.assertRaisesRegex(Exception,'expired'):
            await self.service.buy_from_wallet(telegram_id=123,product_id='basic',
                idempotency_key='buy-expired-discount',discount_code='OLD',now=1000)
        self.assertEqual(self.store.wallet_balance(123),5000)
        self.assertEqual(self.store.get_discount_code('OLD')['used_count'],0)
        self.assertEqual(self.adapter.calls,0)

    async def test_discount_claim_is_released_only_for_proven_pre_provider_failure(self):
        self.store.register_panel_connection(panel_id='rb-disc',label='Rebecca',provider='rebecca',
            base_url='https://rb.example',auth_mode='api_key',secret_ref='env:RB_KEY',enabled=True)
        self.store.put_product(product_id='rb-disc-product',title='Rebecca product',price_minor=1000,
            panel_id='rb-disc',group_ids=[])
        self.store.put_discount_code(code='ROLLBACK',percentage=10,max_uses=1,
            allowed_product_ids=['rb-disc-product'])
        rebecca=FakeAdapter(); rebecca.kind='rebecca'; self.registry.register(rebecca)
        service=CustomerPurchaseService(self.store,secret_resolver=lambda _:'secret',registry=self.registry)
        with self.assertRaisesRegex(Exception,'reservation was reversed'):
            await service.buy_from_wallet(telegram_id=123,product_id='rb-disc-product',
                idempotency_key='buy-preprovider-discount',discount_code='ROLLBACK',now=1000)
        self.assertEqual(self.store.get_discount_code('ROLLBACK')['used_count'],0)
        self.assertEqual(self.store.wallet_balance(123),5000)
        self.assertEqual(rebecca.calls,0)

    async def test_discount_idempotency_key_cannot_be_reused_with_another_code(self):
        self.store.put_discount_code(code='SAVE10',percentage=10)
        self.store.put_discount_code(code='SAVE20',percentage=20)
        await self.service.buy_from_wallet(telegram_id=123,product_id='basic',
            idempotency_key='buy-idem-discount',discount_code='SAVE10',now=1000)
        with self.assertRaisesRegex(Exception,'different discount code'):
            await self.service.buy_from_wallet(telegram_id=123,product_id='basic',
                idempotency_key='buy-idem-discount',discount_code='SAVE20',now=1000)
        self.assertEqual(self.adapter.calls,1)
        self.assertEqual(self.store.wallet_balance(123),2750)

    async def test_last_discount_use_is_claimed_once_under_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor
        self.store.put_discount_code(code='LASTONE',percentage=20,max_uses=1)
        def reserve(index):
            return self.store.reserve_product_order(order_id=f'order-{index}',telegram_id=123,
                product_id='basic',idempotency_key=f'concurrent-{index}',
                discount_code='LASTONE',now=1000)
        def attempt(index):
            try:
                return ('ok',reserve(index))
            except Exception as exc:
                return ('error',str(exc))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(attempt,[1,2]))
        self.assertEqual(sum(1 for status,_ in outcomes if status=='ok'),1)
        self.assertEqual(self.store.get_discount_code('LASTONE')['used_count'],1)
        self.assertEqual(self.store.wallet_balance(123),3000)

    async def test_discount_codes_are_isolated_by_tenant(self):
        other=CustomerDataStore(self.store.db_path,tenant_id='t2')
        self.assertIsNone(other.get_discount_code('SAVE50'))
        self.store.put_discount_code(code='TENANTONLY',percentage=10)
        self.assertIsNone(other.get_discount_code('TENANTONLY'))
