import importlib.util
import tempfile
import unittest
from pathlib import Path

from manubisguard.customer_store import CustomerDataStore
from manubisguard.notifications.trust_channel import SQLiteNotificationOutbox, TrustEvent

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_billing_once.py"
SPEC = importlib.util.spec_from_file_location("anubis_run_billing_once", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class FakeSender:
    def __init__(self): self.messages = []
    async def send_message(self, chat_id, text): self.messages.append((chat_id, text))


class BillingScriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "customer.sqlite3")
        self.store = CustomerDataStore(self.db, tenant_id="test-tenant")
        self.store.set_trust_channel_config(bot_instance_id="customer-test", chat_id="@trust_test",
            enabled=True, enabled_events=[TrustEvent.WALLET_DEPOSIT.value], privacy_mode=True)
        self.outbox = SQLiteNotificationOutbox(self.db)
        config = self.store.get_trust_channel_config("test-tenant", "customer-test")
        self.outbox.enqueue(config, TrustEvent.WALLET_DEPOSIT,
            {"public_order_id":"PAY123", "amount_minor":100, "currency":"IRR", "password":"must-not-appear"},
            idempotency_key="test-wallet-credit", now=100)
        self.sender = FakeSender()
    def tearDown(self): self.tmp.cleanup()

    async def test_billing_runner_delivers_transactional_trust_outbox(self):
        result = await MODULE.deliver_trust_outbox(self.store, token="test-token", bot_instance_id="customer-test",
            sender_lookup=lambda tenant, bot: self.sender if (tenant, bot) == ("test-tenant", "customer-test") else None)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIn("PAY123", self.sender.messages[0][1])
        self.assertNotIn("must-not-appear", self.sender.messages[0][1])
        row = self.outbox.get(1)
        self.assertEqual(row.status, "sent")

    async def test_billing_runner_does_not_mark_outbox_sent_without_token(self):
        result = await MODULE.deliver_trust_outbox(self.store, token="", bot_instance_id="customer-test")
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.outbox.get(1).status, "pending")


if __name__ == "__main__": unittest.main()
