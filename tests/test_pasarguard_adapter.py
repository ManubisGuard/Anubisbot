from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from manubisguard.panels.adapters.pasarguard import PasarGuardAdapter
from manubisguard.panels.contracts import PanelConnection, PanelError, PanelUserNotFound, UserCreate, UserUpdate


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.get_groups_calls = 0

    async def get_groups_simple(self, **kwargs):
        self.get_groups_calls += 1
        return []

    async def get_token(self, *, username, password):
        return SimpleNamespace(access_token="test-access-token")

    async def get_roles_simple(self, **kwargs):
        return [{"id": 3, "name": "Reseller", "is_owner": False},
                {"id": 1, "name": "Owner", "is_owner": True}]

    async def get_users(self, **kwargs):
        self.last_users_kwargs = kwargs
        return SimpleNamespace(users=[
            {"id": 17, "username": "alice", "status": "active", "used_traffic": 3},
            {"id": 18, "username": "bob", "status": "on_hold", "used_traffic": 4},
        ])

    async def get_user_by_id(self, *, user_id, token):
        self.usage_request = {"user_id": user_id, "token": token}
        return SimpleNamespace(id=user_id, used_traffic=12345, data_limit=50000, model_dump=lambda: {
            "id": user_id, "used_traffic": 12345, "data_limit": 50000,
        })

    async def get_user_usage_by_id(self, *, user_id, token):
        self.timeseries_request = {"user_id": user_id, "token": token}
        return SimpleNamespace(stats={"all": [SimpleNamespace(total_traffic=100)]})

    async def create_admin(self, **kwargs): return SimpleNamespace(id=1, username="reseller")
    async def get_admins(self, **kwargs): return SimpleNamespace(admins=[])
    async def modify_admin_by_id(self, **kwargs): return None
    async def remove_admin_by_id(self, **kwargs): return None

    async def create_user(self, *, user, token):
        self.created_user = user
        self.created_token = token
        return {"id": 29, "username": user.username, "status": "active"}

    async def modify_user_by_id(self, *, user_id, user, token):
        self.modified_user_id = user_id
        self.modified_user = user
        self.modified_token = token
        return {"id": user_id, "username": "alice", "status": getattr(user, "status", "active"),
                "data_limit": getattr(user, "data_limit", None), "expire": getattr(user, "expire", None)}

    async def remove_user_by_id(self, *, user_id, token):
        self.deleted_user_id = user_id
        self.deleted_token = token

    async def get_user_by_username(self, *, username, token):
        return {
            "id": 17,
            "username": username,
            "status": "active",
            "used_traffic": 2048,
            "data_limit": 8192,
            "expire": 1_800_000_000,
            "subscription_url": "/sub/secret-path",
        }


class PasarGuardAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = PanelConnection(
            id="panel-a",
            tenant_id="tenant-a",
            name="Test panel",
            kind="pasarguard",
            base_url="https://panel.example.test/",
            auth_mode="api_key",
            secret_ref="secret://tenant-a/panel-a",
        )
        self.clients = []

        def factory(**kwargs):
            client = FakeClient(**kwargs)
            self.clients.append(client)
            return client

        self.adapter = PasarGuardAdapter(
            secret_resolver=lambda ref: "test-secret" if ref == self.connection.secret_ref else "",
            sdk_factory=factory,
        )

    async def test_health_check_uses_authenticated_low_impact_endpoint(self):
        result = await self.adapter.health_check(self.connection)
        self.assertTrue(result["ok"])
        self.assertEqual(result["connection_id"], "panel-a")
        self.assertNotIn("test-secret", repr(result))
        self.assertEqual(self.clients[0].kwargs["api_key"], "test-secret")
        self.assertIs(self.clients[0].kwargs["verify"], True)
        self.assertEqual(self.clients[0].kwargs["timeout"], self.connection.timeout_seconds)
        self.assertEqual(self.clients[0].get_groups_calls, 1)

    async def test_username_password_auth_keeps_tls_and_timeout_after_token_exchange(self):
        connection = PanelConnection(
            id="panel-login",
            tenant_id="tenant-a",
            name="Password-auth panel",
            kind="pasarguard",
            base_url="https://panel.example.test",
            auth_mode="username_password",
            secret_ref="secret://tenant-a/password",
            metadata={"username": "panel-owner"},
            timeout_seconds=7.5,
        )
        clients = []

        def factory(**kwargs):
            client = FakeClient(**kwargs)
            clients.append(client)
            return client

        adapter = PasarGuardAdapter(
            secret_resolver=lambda ref: "panel-password",
            sdk_factory=factory,
        )
        result = await adapter.health_check(connection)

        self.assertTrue(result["ok"])
        self.assertEqual(len(clients), 2)
        self.assertIs(clients[0].kwargs["verify"], True)
        self.assertEqual(clients[0].kwargs["timeout"], 7.5)
        self.assertEqual(clients[1].kwargs["token"], "test-access-token")
        self.assertIs(clients[1].kwargs["verify"], True)
        self.assertEqual(clients[1].kwargs["timeout"], 7.5)

    async def test_list_roles_returns_ids_for_reseller_plan_configuration(self):
        roles = await self.adapter.list_roles(self.connection)
        self.assertEqual(roles, [{"id": 3, "name": "Reseller", "is_owner": False},
                                 {"id": 1, "name": "Owner", "is_owner": True}])

    async def test_capability_report_never_claims_live_panel_verification(self):
        report = await self.adapter.capability_report(self.connection)
        self.assertEqual(report["provider"], "pasarguard")
        self.assertEqual(report["evidence_level"], "local_sdk_introspection_only")
        self.assertFalse(report["live_panel_verified"])
        self.assertIn("create_user", report["operations"])
        self.assertEqual(report["operations"]["list_users"]["status"], "sdk_surface_present")
        self.assertEqual(report["operations"]["create_user"]["live_test"], "not_tested")
        self.assertEqual(report["operations"]["create_user"]["panel_permission"], "unknown")

    async def test_capability_report_detects_actual_native_reseller_sdk_methods_and_models(self):
        fake_sdk = SimpleNamespace(
            AdminCreate=lambda **kwargs: SimpleNamespace(**kwargs),
            AdminModify=lambda **kwargs: SimpleNamespace(**kwargs),
            RoleLimits=lambda **kwargs: SimpleNamespace(**kwargs),
            UserCreate=lambda **kwargs: SimpleNamespace(**kwargs),
            UserModify=lambda **kwargs: SimpleNamespace(**kwargs),
        )
        with patch.dict(sys.modules, {"pasarguard": fake_sdk}):
            report = await self.adapter.capability_report(self.connection)
        self.assertEqual(report["operations"]["manage_resellers"]["status"], "sdk_surface_present")
        self.assertEqual(report["operations"]["manage_resellers"]["panel_permission"], "unknown")
        self.assertEqual(report["operations"]["manage_resellers"]["live_test"], "not_tested")

    async def test_get_user_maps_units_expiry_and_relative_subscription_url(self):
        result = await self.adapter.get_user(self.connection, "alice")
        self.assertEqual(result.provider_user_id, "17")
        self.assertEqual(result.username, "alice")
        self.assertEqual(result.used_bytes, 2048)
        self.assertEqual(result.data_limit_bytes, 8192)
        self.assertEqual(result.subscription_url, "https://panel.example.test/sub/secret-path")
        self.assertEqual(result.expire_at, datetime.fromtimestamp(1_800_000_000, tz=timezone.utc))

    async def test_get_usage_normalizes_byte_counters_for_billing(self):
        usage = await self.adapter.get_usage(self.connection, "17")
        self.assertEqual(usage["used_bytes"], 12345)
        self.assertEqual(usage["data_limit_bytes"], 50000)
        self.assertEqual(self.clients[-1].usage_request["user_id"], 17)
        self.assertEqual(self.clients[-1].usage_request["token"], "test-secret")

    async def test_usage_mapping_fails_closed_when_counter_is_missing(self):
        with self.assertRaisesRegex(PanelError, "omitted a recognized byte counter"):
            self.adapter._map_usage({"username": "alice", "data_limit": 50000})

    async def test_boolean_usage_counters_are_rejected_instead_of_billed_as_one_byte(self):
        with self.assertRaisesRegex(PanelError, "invalid usage counter"):
            self.adapter._map_user({"username": "alice", "used_traffic": True}, self.connection.base_url)
        with self.assertRaisesRegex(PanelError, "invalid data limit"):
            self.adapter._map_user({"username": "alice", "data_limit": False}, self.connection.base_url)
        with self.assertRaisesRegex(PanelError, "invalid usage counter"):
            self.adapter._map_usage({"used_traffic": True})
        with self.assertRaisesRegex(PanelError, "negative usage counter"):
            self.adapter._map_usage({"used_traffic": -1})

    async def test_create_user_supports_current_sdk_method_name(self):
        fake_sdk = SimpleNamespace(UserCreate=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, {"pasarguard": fake_sdk}):
            created = await self.adapter.create_user(
                self.connection,
                UserCreate(username="new-user", data_limit_bytes=4096, group_ids=(2, 5), note="test"),
            )
        self.assertEqual(created.username, "new-user")
        self.assertEqual(created.provider_user_id, "29")
        self.assertEqual(self.clients[-1].created_user.group_ids, [2, 5])
        self.assertEqual(self.clients[-1].created_token, "test-secret")

    async def test_update_and_delete_user_use_provider_id_and_explicit_fields(self):
        fake_sdk = SimpleNamespace(UserModify=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, {"pasarguard": fake_sdk}):
            updated = await self.adapter.update_user(
                self.connection, "17",
                UserUpdate(data_limit_bytes=4096, status="disabled"),
            )
            update_client = self.clients[-1]
            await self.adapter.delete_user(self.connection, "17")
            delete_client = self.clients[-1]
        self.assertEqual(updated.provider_user_id, "17")
        self.assertEqual(update_client.modified_user_id, "17")
        self.assertEqual(update_client.modified_user.data_limit, 4096)
        self.assertEqual(update_client.modified_user.status, "disabled")
        self.assertEqual(delete_client.deleted_user_id, "17")
        self.assertEqual(delete_client.deleted_token, "test-secret")

    async def test_list_users_is_bounded_and_maps_each_user(self):
        users = await self.adapter.list_users(self.connection, limit=5000, offset=-4)
        self.assertEqual([u.username for u in users], ["alice", "bob"])
        self.assertEqual([u.provider_user_id for u in users], ["17", "18"])
        self.assertEqual(self.clients[-1].last_users_kwargs["limit"], 200)
        self.assertEqual(self.clients[-1].last_users_kwargs["offset"], 0)

    async def test_list_users_rejects_success_response_without_users_envelope(self):
        async def malformed_response(self, **kwargs):
            return SimpleNamespace(total=0)

        with patch.object(FakeClient, "get_users", malformed_response):
            with self.assertRaisesRegex(PanelError, "user list response was invalid"):
                await self.adapter.list_users(self.connection)

    async def test_user_mapping_normalizes_boolean_status_and_naive_iso_expiry(self):
        mapped = self.adapter._map_user({
            "id": 19, "username": "charlie", "enable": False,
            "expire": "2030-01-02T03:04:05",
        }, self.connection.base_url)
        self.assertEqual(mapped.status, "disabled")
        self.assertEqual(mapped.expire_at, datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    async def test_disabled_connection_fails_before_resolving_secret(self):
        calls = []
        adapter = PasarGuardAdapter(
            secret_resolver=lambda ref: calls.append(ref) or "secret",
            sdk_factory=lambda **kwargs: FakeClient(**kwargs),
        )
        disabled = PanelConnection(
            id="panel-a", tenant_id="tenant-a", name="disabled", kind="pasarguard",
            base_url="https://panel.example.test", auth_mode="api_key",
            secret_ref="secret://panel", enabled=False,
        )
        result = await adapter.health_check(disabled)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "PanelError")
        self.assertEqual(calls, [])

    async def test_insecure_transport_is_rejected_before_secret_resolution(self):
        calls = []
        adapter = PasarGuardAdapter(
            secret_resolver=lambda ref: calls.append(ref) or "secret",
            sdk_factory=lambda **kwargs: FakeClient(**kwargs),
        )
        insecure = PanelConnection(
            id="panel-a", tenant_id="tenant-a", name="insecure", kind="pasarguard",
            base_url="http://panel.example.test", auth_mode="api_key",
            secret_ref="secret://panel", verify_tls=False,
        )
        result = await adapter.health_check(insecure)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "PanelError")
        self.assertEqual(calls, [])

    async def test_health_check_does_not_echo_provider_exception_or_secret(self):
        class BrokenClient:
            async def get_groups_simple(self, **kwargs):
                raise RuntimeError("private error test-secret")
        adapter = PasarGuardAdapter(
            secret_resolver=lambda ref: "test-secret",
            sdk_factory=lambda **kwargs: BrokenClient(),
        )
        result = await adapter.health_check(self.connection)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("test-secret", repr(result))
        self.assertNotIn("private error", repr(result))


    async def test_get_user_only_maps_definitive_404_to_not_found(self):
        class LookupFailure(Exception):
            def __init__(self, status_code):
                super().__init__("private provider response")
                self.status_code = status_code

        for status in (401, 403, 500):
            with self.subTest(status=status):
                class BrokenClient(FakeClient):
                    async def get_user_by_username(self, *, username, token):
                        raise LookupFailure(status)

                adapter = PasarGuardAdapter(
                    secret_resolver=lambda _: "test-secret",
                    sdk_factory=lambda **kwargs: BrokenClient(**kwargs),
                )
                with self.assertRaises(PanelError) as caught:
                    await adapter.get_user(self.connection, "alice")
                self.assertNotIsInstance(caught.exception, PanelUserNotFound)
                self.assertNotIn("private provider response", str(caught.exception))

        class MissingClient(FakeClient):
            async def get_user_by_username(self, *, username, token):
                raise LookupFailure(404)

        adapter = PasarGuardAdapter(
            secret_resolver=lambda _: "test-secret",
            sdk_factory=lambda **kwargs: MissingClient(**kwargs),
        )
        with self.assertRaises(PanelUserNotFound):
            await adapter.get_user(self.connection, "alice")

    async def test_get_user_rejects_success_response_without_username(self):
        class MalformedClient(FakeClient):
            async def get_user_by_username(self, *, username, token):
                return {"id": 17, "status": "active"}

        adapter = PasarGuardAdapter(
            secret_resolver=lambda _: "test-secret",
            sdk_factory=lambda **kwargs: MalformedClient(**kwargs),
        )
        with self.assertRaisesRegex(PanelError, "omitted user identity"):
            await adapter.get_user(self.connection, "alice")


    async def test_update_and_delete_errors_do_not_chain_provider_details(self):
        class LeakyClient(FakeClient):
            async def modify_user_by_id(self, **kwargs):
                raise RuntimeError("private response contains provider-secret")
            async def remove_user_by_id(self, **kwargs):
                raise RuntimeError("private response contains provider-secret")

        adapter = PasarGuardAdapter(
            secret_resolver=lambda _: "test-secret",
            sdk_factory=lambda **kwargs: LeakyClient(**kwargs),
        )
        fake_sdk = SimpleNamespace(UserModify=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, {"pasarguard": fake_sdk}):
            with self.assertRaisesRegex(PanelError, "update_user failed") as update_error:
                await adapter.update_user(self.connection, "17", UserUpdate(status="disabled"))
            with self.assertRaisesRegex(PanelError, "delete_user failed") as delete_error:
                await adapter.delete_user(self.connection, "17")

        for error in (update_error.exception, delete_error.exception):
            self.assertNotIn("provider-secret", str(error))
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)


    async def test_unsupported_auth_mode_and_missing_login_username_fail_before_secret_resolution(self):
        for connection in (
            replace(self.connection, auth_mode="unsupported"),
            replace(self.connection, auth_mode="username_password", metadata={}),
        ):
            with self.subTest(auth_mode=connection.auth_mode):
                resolved = []
                adapter = PasarGuardAdapter(
                    secret_resolver=lambda ref: resolved.append(ref) or "test-secret",
                    sdk_factory=lambda **kwargs: self.fail("invalid auth config must not construct SDK"),
                )
                report = await adapter.capability_report(connection)
                self.assertFalse(report["live_panel_verified"])
                self.assertEqual(report["error_type"], "PanelError")
                self.assertEqual(resolved, [])
                self.assertTrue(all(op["status"] == "unknown" for op in report["operations"].values()))
                self.assertTrue(all(op["panel_permission"] == "unknown" for op in report["operations"].values()))

    async def test_capability_report_invalid_connection_fails_closed_before_secret_resolution(self):
        resolved = []
        adapter = PasarGuardAdapter(
            secret_resolver=lambda ref: resolved.append(ref) or "test-secret",
            sdk_factory=lambda **kwargs: self.fail("invalid connection must not construct SDK"),
        )
        report = await adapter.capability_report(replace(self.connection, base_url="http://panel.example"))

        self.assertFalse(report["live_panel_verified"])
        self.assertEqual(report["error_type"], "PanelError")
        self.assertEqual(resolved, [])
        self.assertTrue(report["operations"])
        self.assertTrue(all(item["status"] == "unknown" for item in report["operations"].values()))
        self.assertTrue(all(item["panel_permission"] == "unknown" for item in report["operations"].values()))


    async def test_create_and_update_reject_malformed_success_payloads(self):
        class MalformedClient(FakeClient):
            async def create_user(self, *, user, token):
                return {"id": 22, "username": user.username, "used_traffic": "provider-secret"}
            async def modify_user_by_id(self, *, user_id, user, token):
                return {"id": user_id, "username": "alice", "data_limit": -5}

        adapter = PasarGuardAdapter(secret_resolver=lambda _: "test-secret",
            sdk_factory=lambda **kwargs: MalformedClient(**kwargs))
        fake_sdk = SimpleNamespace(
            UserCreate=lambda **kwargs: SimpleNamespace(**kwargs),
            UserModify=lambda **kwargs: SimpleNamespace(**kwargs),
        )
        with patch.dict(sys.modules, {"pasarguard": fake_sdk}):
            with self.assertRaisesRegex(PanelError, "invalid usage counter") as create_error:
                await adapter.create_user(self.connection, UserCreate(username="new-user"))
            with self.assertRaisesRegex(PanelError, "negative data limit") as update_error:
                await adapter.update_user(self.connection, "17", UserUpdate(data_limit_bytes=10))
        for error in (create_error.exception, update_error.exception):
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)
            self.assertNotIn("provider-secret", str(error))

    async def test_user_mapping_rejects_invalid_expiry_and_negative_counters(self):
        for payload, expected in (
            ({"username": "alice", "expire": "not-a-date"}, "invalid expiry"),
            ({"username": "alice", "expire": 10**100}, "invalid expiry"),
            ({"username": "alice", "used_traffic": -1}, "negative usage counter"),
            ({"username": "alice", "data_limit": "provider-secret"}, "invalid data limit"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(PanelError, expected) as caught:
                    self.adapter._map_user(payload, self.connection.base_url)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn("provider-secret", str(caught.exception))

    async def test_ambiguous_mutation_timeouts_are_sanitized_and_never_retried(self):
        calls = {"create": 0, "update": 0, "delete": 0}

        class TimeoutClient(FakeClient):
            async def create_user(self, *, user, token):
                calls["create"] += 1
                raise TimeoutError("provider-secret timeout after write")
            async def modify_user_by_id(self, **kwargs):
                calls["update"] += 1
                raise TimeoutError("provider-secret timeout after write")
            async def remove_user_by_id(self, **kwargs):
                calls["delete"] += 1
                raise TimeoutError("provider-secret timeout after write")

        adapter = PasarGuardAdapter(secret_resolver=lambda _: "test-secret",
            sdk_factory=lambda **kwargs: TimeoutClient(**kwargs))
        fake_sdk = SimpleNamespace(
            UserCreate=lambda **kwargs: SimpleNamespace(**kwargs),
            UserModify=lambda **kwargs: SimpleNamespace(**kwargs),
        )
        with patch.dict(sys.modules, {"pasarguard": fake_sdk}):
            with self.assertRaisesRegex(PanelError, "create_user failed") as create_error:
                await adapter.create_user(self.connection, UserCreate(username="new-user"))
            with self.assertRaisesRegex(PanelError, "update_user failed") as update_error:
                await adapter.update_user(self.connection, "17", UserUpdate(status="disabled"))
            with self.assertRaisesRegex(PanelError, "delete_user failed") as delete_error:
                await adapter.delete_user(self.connection, "17")

        self.assertEqual(calls, {"create": 1, "update": 1, "delete": 1})
        for error in (create_error.exception, update_error.exception, delete_error.exception):
            self.assertNotIn("provider-secret", str(error))
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
