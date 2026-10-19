import asyncio, json, tempfile, unittest
from pathlib import Path
from manubisguard.notifications.trust_channel import SQLiteNotificationOutbox, TrustChannelConfig, TrustEvent, TrustChannelDeliveryWorker, render_trust_message

class TrustChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.outbox=SQLiteNotificationOutbox(Path(self.tmp.name)/"outbox.sqlite3")
        self.config=TrustChannelConfig("tenant-a","bot-a","-1001234567890",enabled_events=frozenset({TrustEvent.NEW_SUBSCRIPTION,TrustEvent.RENEWAL}))
    def test_enqueue_is_idempotent(self):
        a=self.outbox.enqueue(self.config,TrustEvent.NEW_SUBSCRIPTION,{"public_order_id":"ORD_1"},idempotency_key="order:1",now=10)
        b=self.outbox.enqueue(self.config,TrustEvent.NEW_SUBSCRIPTION,{"public_order_id":"ORD_1"},idempotency_key="order:1",now=11)
        self.assertEqual(a,b); self.assertEqual(self.outbox.get(a).status,"pending")
    def test_disabled_event_is_not_enqueued(self):
        self.assertIsNone(self.outbox.enqueue(self.config,TrustEvent.WALLET_DEPOSIT,{"amount_minor":100},idempotency_key="wallet:1",now=10))
    def test_payload_allowlist_drops_secrets(self):
        mid=self.outbox.enqueue(self.config,TrustEvent.NEW_SUBSCRIPTION,{"public_order_id":"ORD_2","subscription_url":"https://secret.invalid/token","password":"secret","token":"secret","amount_minor":1200,"currency":"IRR"},idempotency_key="order:2",now=10)
        payload=json.loads(self.outbox.get(mid).payload_json)
        self.assertEqual(payload,{"public_order_id":"ORD_2","amount_minor":1200,"currency":"IRR"}); self.assertNotIn("secret",self.outbox.get(mid).payload_json)
    def test_stale_processing_claim_is_recovered_after_worker_crash(self):
        mid=self.outbox.enqueue(self.config,TrustEvent.RENEWAL,{},idempotency_key="renew:crash",now=10)
        first=self.outbox.claim_due(now=11)
        self.assertEqual([m.id for m in first],[mid])
        self.assertEqual(first[0].attempts,1)
        recovered=self.outbox.claim_due(now=312)
        self.assertEqual([m.id for m in recovered],[mid])
        self.assertEqual(recovered[0].attempts,2)
        self.assertEqual(self.outbox.get(mid).status,"processing")

    def test_claim_and_mark_sent(self):
        mid=self.outbox.enqueue(self.config,TrustEvent.RENEWAL,{"days":30},idempotency_key="renew:1",now=10)
        self.assertEqual([m.id for m in self.outbox.claim_due(now=10)],[mid]); self.assertEqual(self.outbox.get(mid).status,"processing")
        self.outbox.mark_sent(mid,now=12); self.assertEqual(self.outbox.get(mid).status,"sent")
    def test_retry_backoff_then_dead_letter(self):
        mid=self.outbox.enqueue(self.config,TrustEvent.RENEWAL,{},idempotency_key="renew:2",now=10); self.outbox.claim_due(now=10)
        self.outbox.retry_or_dead(mid,attempt=1,error_code="network_error",now=10,max_attempts=3)
        self.assertEqual(self.outbox.get(mid).available_at,12); self.assertEqual(self.outbox.claim_due(now=11),[]); self.assertEqual(len(self.outbox.claim_due(now=12)),1)
        self.outbox.retry_or_dead(mid,attempt=3,error_code="rate_limited",now=20,max_attempts=3); self.assertEqual(self.outbox.get(mid).status,"dead")
    def test_privacy_renderer_drops_urls_credentials(self):
        msg=render_trust_message(TrustEvent.NEW_SUBSCRIPTION,{"username":"client_1","public_order_id":"ORD-3","subscription_url":"https://secret.invalid","password":"pw","amount_minor":2000,"currency":"IRR"},privacy_mode=True)
        self.assertIn("اشتراک جدید",msg); self.assertIn("ORD-3",msg); self.assertNotIn("client_1",msg); self.assertNotIn("secret.invalid",msg); self.assertNotIn("pw",msg)

    def test_delivery_worker_sends_and_marks_sent(self):
        class Sender:
            def __init__(self): self.messages=[]
            async def send_message(self, chat_id, text): self.messages.append((chat_id,text))
        sender=Sender()
        mid=self.outbox.enqueue(self.config,TrustEvent.NEW_SUBSCRIPTION,{"public_order_id":"ORD_10"},idempotency_key="order:10",now=10)
        worker=TrustChannelDeliveryWorker(self.outbox,lambda tenant,bot:self.config,lambda tenant,bot:sender)
        result=asyncio.run(worker.deliver_batch(now=10))
        self.assertEqual(result["sent"],1); self.assertEqual(self.outbox.get(mid).status,"sent")
        self.assertEqual(sender.messages[0][0],self.config.chat_id); self.assertIn("ORD_10",sender.messages[0][1])

    def test_delivery_worker_retries_without_leaking_exception_text(self):
        class Sender:
            async def send_message(self, chat_id, text): raise RuntimeError("https://secret.invalid/token")
        mid=self.outbox.enqueue(self.config,TrustEvent.NEW_SUBSCRIPTION,{},idempotency_key="order:11",now=10)
        worker=TrustChannelDeliveryWorker(self.outbox,lambda tenant,bot:self.config,lambda tenant,bot:Sender())
        result=asyncio.run(worker.deliver_batch(now=10))
        row=self.outbox.get(mid)
        self.assertEqual(result["retry"],1); self.assertEqual(row.status,"pending")
        self.assertNotIn("secret.invalid",row.payload_json)

    def test_delivery_worker_dead_letters_when_channel_disabled(self):
        mid=self.outbox.enqueue(self.config,TrustEvent.NEW_SUBSCRIPTION,{},idempotency_key="order:12",now=10)
        disabled=TrustChannelConfig("tenant-a","bot-a","-1001234567890",enabled=False,enabled_events=self.config.enabled_events)
        worker=TrustChannelDeliveryWorker(self.outbox,lambda tenant,bot:disabled,lambda tenant,bot:None,max_attempts=2)
        result=asyncio.run(worker.deliver_batch(now=10))
        self.assertEqual(result["dead"],1); self.assertEqual(self.outbox.get(mid).status,"dead")

    def test_invalid_chat_target_rejected(self):
        with self.assertRaisesRegex(ValueError,"chat_id"): TrustChannelConfig("t","b","invalid chat")

if __name__=="__main__": unittest.main()
