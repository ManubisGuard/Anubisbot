import json
import tempfile
import unittest
from pathlib import Path
from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelCapabilities, PanelUser, PanelUserNotFound, UserCreate, UserUpdate
from manubisguard.panels.registry import AdapterRegistry
from manubisguard.sales.service import CustomerPurchaseService, PurchaseNeedsReconciliation

class FakeAdapter:
    kind='pasarguard'
    def __init__(self, fail=False):
        self.calls=0; self.fail=fail; self.last_user=None; self.lookup_user=None; self.lookup_error=None
        self.lookup_calls=0
    async def capabilities(self, connection): return PanelCapabilities(create_user=True, read_status=True)
    async def health_check(self, connection): return {"ok": True}
    async def create_user(self, connection, user: UserCreate):
        self.calls+=1; self.last_user=user
        if self.fail: raise TimeoutError('simulated uncertain result')
        self.lookup_user = PanelUser(provider_user_id='provider-id',username=user.username,status='active',
            data_limit_bytes=user.data_limit_bytes,expire_at=user.expire_at,
            subscription_url='https://sub.example/abc',metadata={"group_ids": list(user.group_ids)})
        return self.lookup_user
    async def get_user(self,connection,username):
        self.lookup_calls += 1
        if self.lookup_error: raise self.lookup_error
        if self.lookup_user is None: raise PanelUserNotFound("Provider user was not found")
        return self.lookup_user
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

    async def test_purchase_idempotency_key_cannot_be_reused_for_another_product(self):
        first = await self.service.buy_from_wallet(telegram_id=123, product_id='basic', idempotency_key='same-key', now=1000)
        with self.assertRaisesRegex(Exception, 'different request'):
            await self.service.buy_from_wallet(telegram_id=123, product_id='another-product', idempotency_key='same-key', now=1000)
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.store.wallet_balance(123), 2500)
        self.assertEqual(self.store.get_order_by_idempotency('same-key', 123)['order_id'], first.order_id)

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
        with self.assertRaisesRegex(Exception,'different request'):
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


    async def _create_ambiguous_order(self, *, key="buy-reconcile"):
        self.adapter.fail = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.buy_from_wallet(telegram_id=123, product_id="basic",
                idempotency_key=key, now=1000)
        order = self.store.get_order_by_idempotency(key, 123)
        self.assertIsNotNone(order)
        self.assertTrue(str(order.get("provider_reference", "")).startswith("reconcile_required:"))
        return order

    async def test_product_order_reconciliation_inspects_matching_target_and_atomically_finalizes(self):
        order = await self._create_ambiguous_order()
        stored = self.store.get_product_order_reconciliation(order["order_id"])
        intent = stored["product_snapshot"]["_provisioning_intent"]
        self.adapter.lookup_user = PanelUser(
            provider_user_id="provider-recovered", username=intent["username"], status="active",
            data_limit_bytes=intent["data_limit_bytes"],
            expire_at=__import__("datetime").datetime.fromisoformat(intent["expire_at"]) if intent["expire_at"] else None,
            subscription_url="https://sub.example/recovered",
            metadata={"group_ids": intent["group_ids"]},
        )
        before_balance = self.store.wallet_balance(123)
        inspected = await self.service.inspect_product_order_reconciliation(order_id=order["order_id"])
        self.assertEqual(inspected["comparison"], "target_present")
        self.assertTrue(inspected["identity_verified"])
        self.assertEqual(inspected["username"], intent["username"])
        result = await self.service.finalize_product_order_target_present(
            order_id=order["order_id"], actor_telegram_id=6883349141,
            reason="Read-only inspection confirmed the exact stored provider target",
        )
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(self.store.get_order_by_idempotency("buy-reconcile", 123)["status"], "active")
        service = self.store.get_customer_service(order["order_id"], 123)
        self.assertEqual(service["provider_user_id"], "provider-recovered")
        self.assertEqual(service["username"], intent["username"])
        self.assertEqual(self.store.wallet_balance(123), before_balance)
        self.assertEqual(self.adapter.calls, 1, "reconciliation must never create a second provider user")
        replay = await self.service.finalize_product_order_target_present(
            order_id=order["order_id"], actor_telegram_id=6883349141,
            reason="Read-only inspection confirmed the exact stored provider target",
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.store.wallet_balance(123), before_balance)
        events = self.store.list_product_order_reconciliation_events(order["order_id"])
        self.assertEqual(len(events), 1)
        with self.store._connect() as db:
            with self.assertRaisesRegex(Exception, "append-only"):
                db.execute("UPDATE customer_product_order_reconciliation_events SET reason='changed' WHERE tenant_id=? AND order_id=?",
                    (self.store.tenant_id, order["order_id"]))
            with self.assertRaisesRegex(Exception, "append-only"):
                db.execute("DELETE FROM customer_product_order_reconciliation_events WHERE tenant_id=? AND order_id=?",
                    (self.store.tenant_id, order["order_id"]))
        other = CustomerDataStore(self.store.db_path, tenant_id="different-tenant")
        self.assertEqual(other.list_product_order_reconciliation_events(order["order_id"]), [])
        restarted_service = CustomerPurchaseService(
            CustomerDataStore(self.store.db_path, tenant_id=self.store.tenant_id),
            secret_resolver=lambda _: "secret", registry=self.registry)
        restarted_replay = await restarted_service.finalize_product_order_target_present(
            order_id=order["order_id"], actor_telegram_id=6883349141,
            reason="Read-only inspection confirmed the exact stored provider target")
        self.assertTrue(restarted_replay["idempotent_replay"])
        self.assertEqual(self.adapter.calls, 1, "restart/replay must not create a provider user again")
        self.assertEqual(self.store.wallet_balance(123), before_balance)

    async def test_product_order_reconciliation_not_found_fails_closed_without_refund_or_retry(self):
        order = await self._create_ambiguous_order(key="buy-reconcile-missing")
        self.adapter.lookup_error = PanelUserNotFound("not found")
        before_balance = self.store.wallet_balance(123)
        inspected = await self.service.inspect_product_order_reconciliation(order_id=order["order_id"])
        self.assertEqual(inspected["comparison"], "not_found")
        with self.assertRaisesRegex(Exception, "not_found"):
            await self.service.finalize_product_order_target_present(
                order_id=order["order_id"], actor_telegram_id=6883349141, reason="Trying safe finalization")
        self.assertEqual(self.store.get_order_by_idempotency("buy-reconcile-missing", 123)["status"], "provisioning")
        self.assertEqual(self.store.wallet_balance(123), before_balance)
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.store.list_product_order_reconciliation_events(order["order_id"]), [])

    async def test_product_order_reconciliation_read_error_fails_closed(self):
        order = await self._create_ambiguous_order(key="buy-reconcile-readerror")
        self.adapter.lookup_error = TimeoutError("read timeout")
        inspected = await self.service.inspect_product_order_reconciliation(order_id=order["order_id"])
        self.assertEqual(inspected["comparison"], "read_failed")
        self.assertEqual(inspected["reason_code"], "TimeoutError")
        self.assertFalse(inspected.get("identity_verified", False))
        with self.assertRaisesRegex(Exception, "read_failed"):
            await self.service.finalize_product_order_target_present(
                order_id=order["order_id"], actor_telegram_id=6883349141, reason="Read timed out")
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.store.get_order_by_idempotency("buy-reconcile-readerror", 123)["status"], "provisioning")

    async def test_product_order_reconciliation_identity_or_configuration_mismatch_fails_closed(self):
        order = await self._create_ambiguous_order(key="buy-reconcile-mismatch")
        stored = self.store.get_product_order_reconciliation(order["order_id"])
        intent = stored["product_snapshot"]["_provisioning_intent"]
        self.adapter.lookup_user = PanelUser(
            provider_user_id="provider-other", username=intent["username"] + "-other", status="active",
            data_limit_bytes=intent["data_limit_bytes"],
            expire_at=__import__("datetime").datetime.fromisoformat(intent["expire_at"]) if intent["expire_at"] else None,
            metadata={"group_ids": intent["group_ids"]},
        )
        inspected = await self.service.inspect_product_order_reconciliation(order_id=order["order_id"])
        self.assertEqual(inspected["comparison"], "identity_mismatch")
        with self.assertRaisesRegex(Exception, "identity_mismatch"):
            await self.service.finalize_product_order_target_present(
                order_id=order["order_id"], actor_telegram_id=6883349141, reason="Wrong user")
        self.assertEqual(self.store.get_order_by_idempotency("buy-reconcile-mismatch", 123)["status"], "provisioning")
        self.assertEqual(self.store.wallet_balance(123), 2500)

    async def test_sanaei_reconciliation_uses_hashed_client_ids_and_keeps_them_out_of_audit(self):
        self.store.register_panel_connection(panel_id="s1", label="Sanaei", provider="sanaei",
            base_url="https://sanaei.example", auth_mode="access_token", secret_ref="env:SANAEI_TOKEN",
            enabled=True, metadata={"inbound_ids": [1], "default_flow": "vision",
                                    "sub_id": "private-sub-id", "ip_limit": 2})
        self.store.put_product(product_id="sanaei-basic", title="Sanaei Basic", price_minor=1000,
            panel_id="s1", traffic_bytes=1024**3, duration_hours=24, group_ids=["1"])
        adapter = FakeAdapter(fail=True)
        adapter.kind = "sanaei"
        registry = AdapterRegistry()
        registry.register(adapter)
        service = CustomerPurchaseService(self.store, secret_resolver=lambda _: "secret", registry=registry)
        with self.assertRaises(PurchaseNeedsReconciliation):
            await service.buy_from_wallet(telegram_id=123, product_id="sanaei-basic",
                idempotency_key="sanaei-recovery", now=1000)
        order = self.store.get_order_by_idempotency("sanaei-recovery", 123)
        intent = self.store.get_product_order_reconciliation(order["order_id"])["reconciliation_intent"]
        created = adapter.last_user
        actual_uuid = created.extra["uuid"]
        actual_sub_id = created.extra["sub_id"]
        self.assertNotIn(actual_uuid, order["product_snapshot_json"])
        self.assertNotIn(actual_sub_id, order["product_snapshot_json"])
        self.assertNotIn("env:SANAEI_TOKEN", order["product_snapshot_json"])
        self.assertEqual(intent["extra"]["uuid_sha256"],
            __import__("hashlib").sha256(actual_uuid.encode()).hexdigest())
        self.assertEqual(intent["extra"]["sub_id_sha256"],
            __import__("hashlib").sha256(actual_sub_id.encode()).hexdigest())
        adapter.lookup_user = PanelUser(
            provider_user_id=created.username, username=created.username, status="active",
            data_limit_bytes=created.data_limit_bytes, expire_at=created.expire_at,
            subscription_url="https://sub.example/recovered",
            metadata={"uuid": actual_uuid, "inbound_ids": [1], "sub_id": actual_sub_id,
                      "ip_limit": 2, "flow": "vision", "protocol": "vless"},
        )
        result = await service.inspect_product_order_reconciliation(order_id=order["order_id"])
        self.assertEqual(result["comparison"], "target_present")
        rendered_evidence = json.dumps(result["evidence"], sort_keys=True)
        self.assertNotIn(actual_uuid, rendered_evidence)
        self.assertNotIn(actual_sub_id, rendered_evidence)
        finalized = await service.finalize_product_order_target_present(order_id=order["order_id"],
            actor_telegram_id=6883349141, reason="Sanaei client identity matched after read-only inspection")
        self.assertFalse(finalized["idempotent_replay"])
        self.assertEqual(self.store.get_order_by_idempotency("sanaei-recovery", 123)["status"], "active")
        with self.store._connect() as db:
            audit_row = db.execute(
                "SELECT evidence_json FROM customer_product_order_reconciliation_events WHERE tenant_id=? AND order_id=?",
                (self.store.tenant_id, order["order_id"])).fetchone()
        self.assertIsNotNone(audit_row)
        self.assertNotIn(actual_uuid, audit_row["evidence_json"])
        self.assertNotIn(actual_sub_id, audit_row["evidence_json"])

    async def test_product_order_reconciliation_is_tenant_scoped_and_intent_precedes_provider_call(self):
        self.adapter.fail = False
        outcome = await self.service.buy_from_wallet(
            telegram_id=123, product_id="basic", idempotency_key="buy-intent-first", now=1000)
        order = self.store.get_product_order_reconciliation(outcome.order_id)
        self.assertIsNotNone(order)
        self.assertEqual(order["product_snapshot"]["_provisioning_intent"]["username"],
            f"u123_{outcome.order_id[:8]}")
        other = CustomerDataStore(self.store.db_path, tenant_id="other-tenant")
        self.assertIsNone(other.get_product_order_reconciliation(outcome.order_id))
        self.assertEqual(other.list_product_order_reconciliation_events(outcome.order_id), [])
