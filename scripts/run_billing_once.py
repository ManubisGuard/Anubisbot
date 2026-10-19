#!/usr/bin/env python3
"""Run one idempotent billing/suspension/expiry pass for the customer bot.

Install as a separate systemd timer process; do not run this against production until the
panel connections, env credentials, and wallet units have been verified in staging.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from manubisguard.customer_store import CustomerDataStore
from manubisguard.sales.billing_worker import CustomerBillingWorker
from manubisguard.resellers.billing_worker import ResellerBillingWorker
from manubisguard.notifications.trust_channel import SQLiteNotificationOutbox, TrustChannelDeliveryWorker
from manubisguard.telegram.control_bot import TelegramBotAPI


class _TrustSender:
    def __init__(self, api: TelegramBotAPI): self.api = api
    async def send_message(self, chat_id: str, text: str) -> None:
        target = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        await self.api.send_text(target, text)


async def deliver_trust_outbox(store: CustomerDataStore, *, token: str | None = None,
                               bot_instance_id: str | None = None,
                               sender_lookup=None) -> dict[str, int]:
    """Drain the transactional notification outbox in this isolated bot's timer process."""
    instance = (bot_instance_id or os.environ.get("ANUBIS_CUSTOMER_BOT_INSTANCE", "customer")).strip() or "customer"
    token = (token if token is not None else os.environ.get("MANUBISGUARD_CUSTOMER_BOT_TOKEN", "")).strip()
    if sender_lookup is None:
        if not token:
            return {"sent": 0, "retry": 0, "dead": 0, "skipped": 1}
        api = TelegramBotAPI(SimpleNamespace(token=token, request_timeout=20.0))
        sender = _TrustSender(api)
        sender_lookup = lambda tenant, bot: sender if tenant == store.tenant_id and bot == instance else None
    outbox = SQLiteNotificationOutbox(store.db_path)
    worker = TrustChannelDeliveryWorker(outbox, store.get_trust_channel_config, sender_lookup)
    return await worker.deliver_batch(limit=100)


def make_secret_resolver(store: CustomerDataStore):
    def resolve_secret(secret_ref: str) -> str:
        if secret_ref.startswith("vault:"):
            account_id = secret_ref[6:]
            credential = store.get_reseller_credential(account_id)
            if not credential:
                raise ValueError("Encrypted reseller credential is unavailable")
            return credential["password"]
        if not secret_ref.startswith("env:"):
            raise ValueError("Only env:VARIABLE_NAME and vault:ACCOUNT_ID secret references are supported")
        name = secret_ref[4:]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            raise ValueError("Invalid environment secret reference")
        value = os.environ.get(name, "")
        if not value:
            raise ValueError(f"Referenced environment variable {name} is empty")
        return value
    return resolve_secret


async def main() -> int:
    store = CustomerDataStore(
        db_path=os.environ.get("ANUBIS_CUSTOMER_DB"),
        tenant_id=os.environ.get("ANUBIS_CUSTOMER_TENANT", "default"),
    )
    now = int(time.time())
    resolve_secret = make_secret_resolver(store)
    customer = CustomerBillingWorker(store, secret_resolver=resolve_secret)
    reseller = ResellerBillingWorker(store, secret_resolver=resolve_secret)
    customer_result = await customer.run_once(now=now, limit=200)
    reseller_result = await reseller.run_once(now=now, limit=200)
    trust_result = await deliver_trust_outbox(store)
    # Do not print usernames, IDs, panel URLs, credentials, or provider response bodies.
    print(json.dumps({"customer_billing": customer_result, "reseller_billing": reseller_result,
                      "trust_outbox": trust_result}, sort_keys=True))
    errors = sum(v for k, v in customer_result.items() if k.endswith("failed"))
    errors += sum(v for k, v in reseller_result.items() if k.endswith("failed"))
    errors += trust_result.get("dead", 0)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
