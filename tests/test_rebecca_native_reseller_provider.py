import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelError
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.providers import RebeccaNativeResellerProvider


class Response:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data
        self.content = json.dumps(data).encode() if data is not None else b""
    def json(self): return self._data


class FakeClient:
    def __init__(self, handler, **kwargs): self.handler = handler
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def request(self, method, path, **kwargs): return self.handler(method, path, kwargs)


class RebeccaNativeResellerProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustomerDataStore(str(Path(self.tmp.name) / "db.sqlite3"), tenant_id="t")
        self.store.register_panel_connection(panel_id="rb", label="Rebecca staging", provider="rebecca",
            base_url="https://rb.example", auth_mode="api_key", secret_ref="env:RB_KEY", enabled=True,
            metadata={"service_id": 12})
        self.calls = []
        self.admins = []
        self.paths = {
            "/admin": {"post": {}}, "/admins": {"get": {}},
            "/admin/{username}": {"put": {}, "delete": {}},
            "/admin/{username}/disable": {"post": {}},
            "/admin/{username}/enable": {"post": {}},
        }
        self.schema = {"openapi": "3.0.3", "info": {"version": "1.7.0"}, "paths": self.paths}
        def handler(method, path, kwargs):
            self.calls.append((method, path, kwargs))
            if path == "/openapi.json": return Response(200, self.schema)
            if path == "/api/v2/services": return Response(200, {"services": [{"id": 12}, {"id": 13}]})
            if path.startswith("/api/admins?"):
                query = parse_qs(urlsplit(path).query)
                username = query.get("username", [""])[0]
                rows = [row for row in self.admins if not username or row["username"].casefold() == username.casefold()]
                return Response(200, {"admins": rows, "total": len(rows)})
            if path == "/api/admin" and method == "POST":
                row = {"id": len(self.admins) + 1, "username": kwargs["json"]["username"],
                       "role": kwargs["json"]["role"], "status": "active", "users_usage": 0}
                self.admins.append(row)
                return Response(200, row)
            if path.endswith("/disable") or path.endswith("/enable") or (path.startswith("/api/admin/") and method in {"DELETE", "PUT"}):
                return Response(204)
            return Response(404, {"detail": "not found"})
        self.handler = handler
        self.provider = RebeccaNativeResellerProvider(self.store, secret_resolver=lambda _: "test-key",
            client_factory=lambda **kw: FakeClient(handler, **kw))
        self.plan = ResellerPlan(plan_id="rp", panel_id="rb", pricing_mode=PricingMode.FIXED,
            price=Decimal(100), data_limit_bytes=1024**3, max_users=20,
            allowed_group_ids=("12", "13"), currency="IRR")

    def tearDown(self): self.tmp.cleanup()

    async def test_native_create_read_status_and_delete_contract(self):
        await self.provider.validate_plan(panel_id="rb", username="reseller1", plan=self.plan)
        created = await self.provider.create_reseller(panel_id="rb", username="reseller1", plan=self.plan,
            owner_id="808", volume_gb=None)
        self.assertEqual(created.provider_account_id, "reseller1")
        self.assertTrue(created.initial_password)
        post = next(call for call in self.calls if call[0] == "POST" and call[1] == "/api/admin")
        payload = post[2]["json"]
        self.assertEqual(payload["role"], "reseller")
        self.assertEqual(payload["services"], [12, 13])
        self.assertEqual(payload["users_limit"], 20)
        self.assertNotIn("permissions", payload)  # Rebecca applies role defaults, not owner privileges.
        admin = await self.provider.get_reseller(panel_id="rb", provider_account_id="reseller1")
        self.assertEqual(admin.status, "active")
        await self.provider.update_reseller_permissions(panel_id="rb", provider_account_id="reseller1", plan=self.plan)
        await self.provider.set_reseller_status(panel_id="rb", provider_account_id="reseller1", enabled=False)
        await self.provider.set_reseller_status(panel_id="rb", provider_account_id="reseller1", enabled=True)
        await self.provider.delete_reseller(panel_id="rb", provider_account_id="reseller1")
        paths = [(method, path) for method, path, _ in self.calls]
        self.assertIn(("PUT", "/api/admin/reseller1"), paths)
        self.assertIn(("POST", "/api/admin/reseller1/disable"), paths)
        self.assertIn(("POST", "/api/admin/reseller1/enable"), paths)
        self.assertIn(("DELETE", "/api/admin/reseller1"), paths)

    async def test_direct_create_cannot_bypass_service_scope_validation(self):
        invalid = ResellerPlan(plan_id="bad-scope", panel_id="rb", pricing_mode=PricingMode.FIXED,
            price=Decimal(100), allowed_group_ids=("999",), currency="IRR")
        with self.assertRaisesRegex(PanelError, "not present on this panel"):
            await self.provider.create_reseller(panel_id="rb", username="reseller-bad", plan=invalid,
                owner_id="808", volume_gb=None)
        self.assertFalse(any(method == "POST" and path == "/api/admin" for method, path, _ in self.calls))

    async def test_direct_create_rejects_unmapped_permission_overrides(self):
        invalid = ResellerPlan(plan_id="bad-perms", panel_id="rb", pricing_mode=PricingMode.FIXED,
            price=Decimal(100), allowed_group_ids=("12",), permission_overrides=(("can_manage_admins", True),),
            currency="IRR")
        with self.assertRaisesRegex(PanelError, "unsupported provider permission overrides"):
            await self.provider.create_reseller(panel_id="rb", username="reseller-bad", plan=invalid,
                owner_id="808", volume_gb=None)
        self.assertFalse(any(method == "POST" and path == "/api/admin" for method, path, _ in self.calls))

    async def test_missing_lifecycle_contract_fails_closed(self):
        self.paths.pop("/admin/{username}/disable")
        with self.assertRaisesRegex(PanelError, "unsupported by this API contract"):
            await self.provider.validate_plan(panel_id="rb", username="reseller2", plan=self.plan)
        self.assertFalse(any(method == "POST" and path == "/api/admin" for method, path, _ in self.calls))

    async def test_missing_service_scope_fails_before_purchase(self):
        self.store.register_panel_connection(panel_id="rb-empty", label="Rebecca no scope", provider="rebecca",
            base_url="https://rb.example", auth_mode="api_key", secret_ref="env:RB_KEY", enabled=True, metadata={})
        plan = ResellerPlan(plan_id="rp2", panel_id="rb-empty", pricing_mode=PricingMode.FIXED,
            price=Decimal(100), currency="IRR")
        with self.assertRaisesRegex(PanelError, "explicitly scoped service ID"):
            await self.provider.validate_plan(panel_id="rb-empty", username="reseller3", plan=plan)

    async def test_forbidden_admin_api_does_not_leak_response(self):
        def denied(method, path, kwargs):
            self.calls.append((method, path, kwargs))
            if path == "/openapi.json": return Response(200, self.schema)
            return Response(403, {"detail": "secret internal error"})
        provider = RebeccaNativeResellerProvider(self.store, secret_resolver=lambda _: "test-key",
            client_factory=lambda **kw: FakeClient(denied, **kw))
        with self.assertRaisesRegex(PanelError, "lacks permission") as ctx:
            await provider.validate_plan(panel_id="rb", username="reseller4", plan=self.plan)
        self.assertNotIn("secret internal error", str(ctx.exception))


if __name__ == "__main__": unittest.main()
