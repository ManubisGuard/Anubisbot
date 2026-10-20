import hashlib
import hmac
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import AsyncMock, Mock

from manubisguard.customer_store import CustomerDataStore, CustomerStoreError
from manubisguard.sales.service import PurchaseOutcome, PurchaseNeedsReconciliation
from manubisguard.web.customer_app import InitDataError, create_customer_miniapp_app, validate_telegram_init_data


BOT_TOKEN = "123456:customer-test-token"
NOW = 1_800_000_000
USER = {"id": 123, "first_name": "Alice", "username": "alice", "language_code": "fa"}


def signed_init_data(user=USER, *, auth_date=NOW):
    fields = {"auth_date": str(auth_date), "query_id": "test-query",
              "user": json.dumps(user, ensure_ascii=False, separators=(",", ":"))}
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class CustomerMiniAppAuthTests(unittest.TestCase):
    def test_valid_init_data_is_verified_and_identity_is_parsed(self):
        verified = validate_telegram_init_data(signed_init_data(), BOT_TOKEN, now=NOW)
        self.assertEqual(verified["telegram_id"], 123)
        self.assertEqual(verified["user"]["username"], "alice")

    def test_invalid_signature_is_rejected(self):
        raw = signed_init_data().replace("Alice", "Mallory")
        with self.assertRaisesRegex(InitDataError, "hash_invalid"):
            validate_telegram_init_data(raw, BOT_TOKEN, now=NOW)

    def test_stale_future_and_malformed_user_data_are_rejected(self):
        with self.assertRaisesRegex(InitDataError, "expired"):
            validate_telegram_init_data(signed_init_data(auth_date=NOW - 301), BOT_TOKEN, now=NOW)
        with self.assertRaisesRegex(InitDataError, "expired"):
            validate_telegram_init_data(signed_init_data(auth_date=NOW + 31), BOT_TOKEN, now=NOW)
        with self.assertRaisesRegex(InitDataError, "user_id_invalid"):
            validate_telegram_init_data(signed_init_data(user={"id": True, "first_name": "bad"}), BOT_TOKEN, now=NOW)

    def test_duplicate_query_fields_are_rejected_even_with_valid_hash(self):
        with self.assertRaisesRegex(InitDataError, "duplicate_field"):
            validate_telegram_init_data(signed_init_data() + "&auth_date=" + str(NOW), BOT_TOKEN, now=NOW)


class CustomerMiniAppWSGITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CustomerDataStore(str(Path(self.tmp.name) / "customer.sqlite3"), tenant_id="tenant-a")
        self.store.ensure_customer(123, "alice", "Alice")
        self.store.ensure_customer(456, "bob", "Bob")
        self.store.post_wallet_entry(telegram_id=123, idempotency_key="alice-deposit", amount_minor=2500, entry_type="deposit")
        self.store.post_wallet_entry(telegram_id=456, idempotency_key="bob-deposit", amount_minor=9000, entry_type="deposit")
        self.store.register_panel_connection(panel_id="panel-secret", label="Panel", provider="pasarguard",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_SECRET", enabled=True)
        self.store.put_product(product_id="basic", title="Basic", price_minor=1000, currency="IRR",
            panel_id="panel-secret", traffic_bytes=1024**3, duration_hours=24, group_ids=["private-group"])
        self.store.put_product(product_id="hidden", title="Hidden", price_minor=999, currency="IRR",
            panel_id="panel-secret", traffic_bytes=1024**3, duration_hours=24, group_ids=["private-group"], enabled=False)
        product = {"product_id":"basic", "panel_id":"panel-secret", "pricing_mode":"fixed",
                   "currency":"IRR", "unit_price_minor":0, "duration_hours":24, "traffic_bytes":1024**3}
        self.store.create_metered_service(service_id="svc-alice", telegram_id=123, product=product,
            provider_user_id="private-provider-id", username="alice", subscription_url="https://sub.example/alice-secret", now=NOW)
        self.store.create_metered_service(service_id="svc-bob", telegram_id=456, product=product,
            provider_user_id="bob-private-id", username="bob", subscription_url="https://sub.example/bob-secret", now=NOW)
        self.app = create_customer_miniapp_app(self.store, BOT_TOKEN, allowed_origins=["https://mini.example"], clock=lambda: NOW)

    def request(self, path, *, init_data=None, method="GET", origin="https://mini.example", body=None, content_type="application/json"):
        captured = {}
        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.url_scheme": "https",
                   "HTTP_ORIGIN": origin or ""}
        if init_data is not None:
            environ["HTTP_AUTHORIZATION"] = "tma " + init_data
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            environ["wsgi.input"] = io.BytesIO(raw_body)
            environ["CONTENT_LENGTH"] = str(len(raw_body))
            environ["CONTENT_TYPE"] = content_type
        raw_response = b"".join(self.app(environ, start_response))
        captured["body"] = json.loads(raw_response.decode("utf-8"))
        return captured

    def test_health_is_public_but_api_requires_signed_init_data(self):
        self.assertEqual(self.request("/healthz")["status"], "200 OK")
        response = self.request("/api/v1/me")
        self.assertEqual(response["status"], "401 Unauthorized")
        self.assertEqual(response["body"], {"error":"unauthorized"})

    def test_origin_is_allowlisted_and_no_cors_read_access_is_granted(self):
        response = self.request("/api/v1/me", init_data=signed_init_data(), origin="https://evil.example")
        self.assertEqual(response["status"], "403 Forbidden")
        self.assertNotIn("Access-Control-Allow-Origin", response["headers"])

    def test_non_get_requests_are_rejected_without_business_side_effects(self):
        response = self.request("/api/v1/wallet", init_data=signed_init_data(), method="POST")
        self.assertEqual(response["status"], "405 Method Not Allowed")
        self.assertEqual(self.store.wallet_balance(123), 2500)
        self.assertEqual(self.request("/api/v1/service-change-preview", init_data=signed_init_data())["status"], "405 Method Not Allowed")

    def test_service_change_preview_is_authenticated_read_only_and_response_allowlisted(self):
        captured = {}
        class FakeChangeService:
            def __init__(self, store, *, secret_resolver, registry):
                captured["store"] = store
                captured["resolver"] = secret_resolver
                captured["registry"] = registry
            async def preview(self, *, telegram_id, service_id, change_type, now):
                captured.update(telegram_id=telegram_id, service_id=service_id, change_type=change_type, now=now)
                return {"service_id": service_id, "change_type": change_type, "amount_minor": 1000,
                    "currency": "IRR", "quantity": 24, "before_expires_at": "2030-01-01T00:00:00+00:00",
                    "target_expires_at": "2030-01-02T00:00:00+00:00",
                    "provider_status": "active", "subscription_url": "https://secret.example/private",
                    "provider_token": "do-not-return"}
        self.app.service_change_service_factory = FakeChangeService
        before_balance = self.store.wallet_balance(123)
        response = self.request("/api/v1/service-change-preview", init_data=signed_init_data(), method="POST",
            body={"service_id": "svc-alice", "change_type": "renewal"})
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(captured["telegram_id"], 123)
        self.assertEqual(captured["service_id"], "svc-alice")
        self.assertEqual(captured["change_type"], "renewal")
        self.assertEqual(captured["now"], NOW)
        self.assertFalse(response["body"]["mutates_provider"])
        self.assertFalse(response["body"]["reserves_wallet"])
        rendered = json.dumps(response["body"])
        self.assertNotIn("secret.example", rendered)
        self.assertNotIn("provider_token", rendered)
        self.assertNotIn("do-not-return", rendered)
        self.assertEqual(self.store.wallet_balance(123), before_balance)

    def test_service_change_preview_rejects_invalid_fields_and_sanitizes_service_errors(self):
        bad_id = self.request("/api/v1/service-change-preview", init_data=signed_init_data(), method="POST",
            body={"service_id": "../svc-alice", "change_type": "renewal"})
        bad_type = self.request("/api/v1/service-change-preview", init_data=signed_init_data(), method="POST",
            body={"service_id": "svc-alice", "change_type": "delete"})
        self.assertEqual(bad_id["status"], "400 Bad Request")
        self.assertEqual(bad_type["status"], "400 Bad Request")
        class FailingChangeService:
            def __init__(self, *args, **kwargs): pass
            async def preview(self, **kwargs): raise RuntimeError("credential=secret-token")
        self.app.service_change_service_factory = FailingChangeService
        failed = self.request("/api/v1/service-change-preview", init_data=signed_init_data(), method="POST",
            body={"service_id": "svc-alice", "change_type": "traffic_topup"})
        self.assertEqual(failed["status"], "502 Bad Gateway")
        self.assertEqual(failed["body"], {"error": "service_change_preview_failed"})

    def test_service_change_execution_requires_signed_quote_owner_and_allowlisted_result(self):
        captured = {"changes": []}
        class FakeChangeService:
            def __init__(self, *args, **kwargs): pass
            async def preview(self, *, telegram_id, service_id, change_type, now):
                return {"service_id": service_id, "change_type": change_type, "amount_minor": 1000,
                    "currency": "IRR", "quantity": 24, "before_expires_at": "2030-01-01T00:00:00+00:00",
                    "target_expires_at": "2030-01-02T00:00:00+00:00", "provider_status": "active"}
            async def change(self, **kwargs):
                captured["changes"].append(kwargs)
                return {"operation_id": "operation-123", "service_id": kwargs["service_id"],
                    "change_type": kwargs["change_type"], "amount_minor": 1000, "currency": "IRR",
                    "quantity": 24, "status": "completed", "expires_at": "2030-01-02T00:00:00+00:00",
                    "provider_token": "must-not-leak"}
        self.app.service_change_service_factory = FakeChangeService
        preview_response = self.request("/api/v1/service-change-preview", init_data=signed_init_data(), method="POST",
            body={"service_id": "svc-alice", "change_type": "renewal"})
        self.assertEqual(preview_response["status"], "200 OK")
        preview = preview_response["body"]["preview"]
        token = preview_response["body"]["quote_token"]
        self.assertIsNone(self.app._verify_service_change_quote(token, telegram_id=123, service_id="svc-alice",
            change_type="renewal", now=NOW + 301))
        quote = {key: preview[key] for key in ("amount_minor", "currency", "quantity", "before_expires_at", "target_expires_at")}
        key = "service-change-test-id-123456"
        tampered_quote = {**quote, "amount_minor": 1}
        tampered = self.request("/api/v1/service-change", init_data=signed_init_data(), method="POST",
            body={"service_id": "svc-alice", "change_type": "renewal", "idempotency_key": key,
                  "quote": tampered_quote, "quote_token": token})
        self.assertEqual(tampered["status"], "409 Conflict")
        self.assertEqual(captured["changes"], [])
        other_user = {"id": 456, "first_name": "Bob", "username": "bob"}
        wrong_owner = self.request("/api/v1/service-change", init_data=signed_init_data(user=other_user), method="POST",
            body={"service_id": "svc-alice", "change_type": "renewal", "idempotency_key": key,
                  "quote": quote, "quote_token": token})
        self.assertEqual(wrong_owner["status"], "409 Conflict")
        self.assertEqual(captured["changes"], [])
        success = self.request("/api/v1/service-change", init_data=signed_init_data(), method="POST",
            body={"service_id": "svc-alice", "change_type": "renewal", "idempotency_key": key,
                  "quote": quote, "quote_token": token})
        self.assertEqual(success["status"], "200 OK")
        self.assertEqual(captured["changes"][0]["telegram_id"], 123)
        self.assertEqual({key: value for key, value in captured["changes"][0]["expected_quote"].items() if key != "quote_issued_at"}, quote)
        self.assertEqual(captured["changes"][0]["expected_quote"]["quote_issued_at"], NOW)
        self.assertEqual(success["body"]["result"]["operation_id"], "operation-123")
        self.assertNotIn("provider_token", json.dumps(success["body"]))

    def test_shop_hides_panel_binding_and_only_returns_enabled_products(self):
        response = self.request("/api/v1/shop", init_data=signed_init_data())
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual([p["product_id"] for p in response["body"]["products"]], ["basic"])
        product = response["body"]["products"][0]
        self.assertNotIn("panel_id", product)
        self.assertNotIn("group_ids_json", product)
        self.assertNotIn("private-group", json.dumps(response["body"]))

    def test_wallet_and_services_are_scoped_to_authenticated_telegram_user(self):
        wallet = self.request("/api/v1/wallet", init_data=signed_init_data())["body"]
        self.assertEqual(wallet["balances"], {"IRR": 2500})
        services = self.request("/api/v1/services", init_data=signed_init_data())["body"]["services"]
        self.assertEqual([item["service_id"] for item in services], ["svc-alice"])
        self.assertEqual(services[0]["subscription_url"], "https://sub.example/alice-secret")
        serialized = json.dumps(services)
        self.assertNotIn("private-provider-id", serialized)
        self.assertNotIn("bob-secret", serialized)

    def test_order_history_is_scoped_and_omits_internal_idempotency_and_provider_data(self):
        self.store.create_order(order_id="alice-order", telegram_id=123, product_id="basic", idempotency_key="alice-secret-idempotency")
        self.store.create_order(order_id="bob-order", telegram_id=456, product_id="basic", idempotency_key="bob-secret-idempotency")
        response = self.request("/api/v1/orders", init_data=signed_init_data())
        self.assertEqual(response["status"], "200 OK")
        orders = response["body"]["orders"]
        self.assertEqual([order["order_id"] for order in orders], ["alice-order"])
        self.assertEqual(orders[0]["title"], "Basic")
        self.assertEqual(orders[0]["status"], "pending")
        serialized = json.dumps(response["body"])
        self.assertNotIn("alice-secret-idempotency", serialized)
        self.assertNotIn("bob-secret-idempotency", serialized)
        self.assertNotIn("provider_reference", serialized)
        self.assertNotIn("bob-order", serialized)

    def test_order_history_rejects_unknown_routes_and_wrong_methods(self):
        response = self.request("/api/v1/orders", init_data=signed_init_data(), method="POST", body={})
        self.assertEqual(response["status"], "405 Method Not Allowed")
        missing = self.request("/api/v1/orders/other", init_data=signed_init_data())
        self.assertEqual(missing["status"], "404 Not Found")

    def test_wallet_transaction_history_is_scoped_and_hides_private_fields(self):
        self.store.post_wallet_entry(telegram_id=123, idempotency_key="alice-private-key", amount_minor=300,
            entry_type="deposit", reference="alice-private-reference", note="alice-private-note")
        self.store.post_wallet_entry(telegram_id=123, idempotency_key="alice-purchase-key", amount_minor=-100,
            entry_type="purchase", reference="alice-private-order", note="private subscription link")
        self.store.post_wallet_entry(telegram_id=456, idempotency_key="bob-private-key", amount_minor=900,
            entry_type="deposit", reference="bob-private-reference", note="bob-private-note")
        response = self.request("/api/v1/transactions", init_data=signed_init_data())
        self.assertEqual(response["status"], "200 OK")
        transactions = response["body"]["transactions"]
        self.assertEqual([item["amount_minor"] for item in transactions], [-100, 300, 2500])
        self.assertTrue(all(item["currency"] == "IRR" for item in transactions))
        serialized = json.dumps(response["body"])
        for private_value in ("alice-private-key", "alice-private-reference", "alice-private-note",
                              "alice-private-order", "private subscription link", "bob-private-reference", "bob-private-note"):
            self.assertNotIn(private_value, serialized)
        self.assertNotIn("idempotency_key", serialized)
        self.assertNotIn("reference", serialized)
        self.assertNotIn("note", serialized)

    def test_wallet_transaction_history_rejects_wrong_methods(self):
        response = self.request("/api/v1/transactions", init_data=signed_init_data(), method="POST", body={})
        self.assertEqual(response["status"], "405 Method Not Allowed")

    def test_support_ticket_create_is_authenticated_idempotent_and_queued_for_admin(self):
        payload = {"subject": "مشکل خرید", "message": "بعد از خرید سرویس در حسابم نمایش داده نمی‌شود.",
                   "idempotency_key": "ticket-request-key-12345"}
        first = self.request("/api/v1/tickets", init_data=signed_init_data(), method="POST", body=payload)
        self.assertEqual(first["status"], "201 Created")
        ticket_id = first["body"]["ticket_id"]
        self.assertEqual(first["body"]["status"], "open")
        second = self.request("/api/v1/tickets", init_data=signed_init_data(), method="POST", body=payload)
        self.assertEqual(second["status"], "201 Created")
        self.assertEqual(second["body"]["ticket_id"], ticket_id)
        own = self.request("/api/v1/tickets", init_data=signed_init_data())
        self.assertEqual(len(own["body"]["tickets"]), 1)
        self.assertEqual(own["body"]["tickets"][0]["subject"], payload["subject"])
        bob = self.request("/api/v1/tickets", init_data=signed_init_data(user={"id":456,"first_name":"Bob"}))
        self.assertEqual(bob["body"]["tickets"], [])
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM customer_tickets WHERE tenant_id='tenant-a'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM customer_ticket_notifications WHERE tenant_id='tenant-a'").fetchone()[0], 1)
            row = db.execute("SELECT body FROM customer_ticket_messages WHERE tenant_id='tenant-a' AND ticket_id=?", (ticket_id,)).fetchone()
        self.assertEqual(row["body"], payload["message"])

    def test_support_ticket_rejects_bad_fields_method_and_origin(self):
        payload = {"subject": "x", "message": "short", "idempotency_key": "ticket-request-key-12345"}
        invalid = self.request("/api/v1/tickets", init_data=signed_init_data(), method="POST", body=payload)
        self.assertEqual(invalid["status"], "400 Bad Request")
        wrong_method = self.request("/api/v1/tickets", init_data=signed_init_data(), method="PUT")
        self.assertEqual(wrong_method["status"], "405 Method Not Allowed")
        denied = self.request("/api/v1/tickets", init_data=signed_init_data(), method="POST", origin="https://evil.example", body={
            "subject":"Valid subject", "message":"This is a valid support message.", "idempotency_key":"ticket-request-key-12345"})
        self.assertEqual(denied["status"], "403 Forbidden")
        self.assertEqual(self.store.list_tickets(123), [])

    def test_wallet_purchase_uses_signed_owner_and_namespaced_idempotency_key(self):
        fake_service = Mock()
        fake_service.buy_from_wallet = AsyncMock(return_value=PurchaseOutcome(
            order_id="order-1", service_id="service-1", status="active", amount_minor=1000,
            currency="IRR", username="u123_order", subscription_url="https://sub.example/owned", provider_user_id="hidden"))
        factory = Mock(return_value=fake_service)
        self.app.purchase_service_factory = factory
        response = self.request("/api/v1/purchase", init_data=signed_init_data(), method="POST",
            body={"product_id":"basic", "idempotency_key":"miniapp-retry-key-12345"})
        self.assertEqual(response["status"], "201 Created")
        self.assertEqual(response["body"]["order_id"], "order-1")
        self.assertNotIn("provider_user_id", response["body"])
        factory.assert_called_once()
        kwargs = fake_service.buy_from_wallet.await_args.kwargs
        self.assertEqual(kwargs["telegram_id"], 123)
        self.assertEqual(kwargs["idempotency_key"], "miniapp:123:miniapp-retry-key-12345")

    def test_purchase_requires_origin_json_and_valid_idempotency_key(self):
        fake_service = Mock()
        fake_service.buy_from_wallet = AsyncMock()
        self.app.purchase_service_factory = Mock(return_value=fake_service)
        denied = self.request("/api/v1/purchase", init_data=signed_init_data(), method="POST", origin="", body={"product_id":"basic", "idempotency_key":"valid-key-123456789"})
        self.assertEqual(denied["status"], "403 Forbidden")
        wrong_type = self.request("/api/v1/purchase", init_data=signed_init_data(), method="POST", body={"product_id":"basic", "idempotency_key":"valid-key-123456789"}, content_type="text/plain")
        self.assertEqual(wrong_type["status"], "415 Unsupported Media Type")
        invalid_key = self.request("/api/v1/purchase", init_data=signed_init_data(), method="POST", body={"product_id":"basic", "idempotency_key":"tiny"})
        self.assertEqual(invalid_key["status"], "400 Bad Request")
        fake_service.buy_from_wallet.assert_not_awaited()

    def test_ambiguous_purchase_returns_accepted_without_blind_retry(self):
        fake_service = Mock()
        async def ambiguous(**kwargs):
            raise PurchaseNeedsReconciliation("do-not-leak-order-id")
        fake_service.buy_from_wallet = ambiguous
        self.app.purchase_service_factory = Mock(return_value=fake_service)
        response = self.request("/api/v1/purchase", init_data=signed_init_data(), method="POST",
            body={"product_id":"basic", "idempotency_key":"miniapp-reconcile-key-12345"})
        self.assertEqual(response["status"], "202 Accepted")
        self.assertEqual(response["body"]["status"], "reconciliation_required")
        self.assertNotIn("do-not-leak-order-id", json.dumps(response["body"]))

    def test_purchase_store_errors_do_not_leak_details(self):
        fake_service = Mock()
        async def rejected(**kwargs):
            raise CustomerStoreError("private database detail")
        fake_service.buy_from_wallet = rejected
        self.app.purchase_service_factory = Mock(return_value=fake_service)
        response = self.request("/api/v1/purchase", init_data=signed_init_data(), method="POST",
            body={"product_id":"basic", "idempotency_key":"miniapp-store-error-12345"})
        self.assertEqual(response["status"], "409 Conflict")
        self.assertEqual(response["body"], {"error":"purchase_rejected"})

    def test_unknown_routes_are_not_exposed(self):
        response = self.request("/api/v1/admin", init_data=signed_init_data())
        self.assertEqual(response["status"], "404 Not Found")


if __name__ == "__main__":
    unittest.main()