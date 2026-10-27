import json
import unittest
from dataclasses import replace
from urllib.parse import urlsplit
from manubisguard.panels.adapters.rebecca import RebeccaAdapter
from manubisguard.panels.contracts import PanelConnection, PanelError, PanelUserNotFound, UserCreate, UserUpdate

class Response:
    def __init__(self, status, data=None, text=''):
        self.status_code=status; self._data=data; self.content=(json.dumps(data).encode() if data is not None else text.encode())
    def json(self):
        if self._data is None: raise ValueError('not json')
        return self._data

class FakeClient:
    def __init__(self, handler, base_url): self.handler=handler; self.base_url=base_url.rstrip('/')
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
    async def request(self, method, path, **kwargs):
        return self.handler(method, path, kwargs)

class RebeccaAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls=[]
        def handler(method,path,kwargs):
            self.calls.append((method,path,kwargs.get('headers',{}).get('Authorization'),kwargs.get('json')))
            if path == '/api/system': return Response(200,{'version':'test'})
            if path == '/api/v2/users': return Response(200,{'username':'alice','service_id':'svc-1','data_limit':100,'status':'active','expire':1800000000,'subscription_urls':{'username-key':'/sub/alice/key123'}})
            if path == '/api/user/alice': return Response(200,{'username':'alice','data_limit':100,'status':'active','links':['/sub/alice/key123']})
            if path == '/api/user/alice/usage': return Response(200,{'used_traffic':42})
            return Response(204)
        self.handler=handler
        self.connection=PanelConnection(id='r1',tenant_id='t1',name='Rebecca',kind='rebecca',base_url='https://rebecca.example',auth_mode='api_key',secret_ref='secret://r1',metadata={'service_id':'svc-1'})
        self.adapter=RebeccaAdapter(secret_resolver=lambda _: 'fake-key',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))

    async def test_capability_report_distinguishes_adapter_methods_from_live_permissions(self):
        report = await self.adapter.capability_report(self.connection)
        self.assertEqual(report["provider"], "rebecca")
        self.assertFalse(report["live_panel_verified"])
        self.assertTrue(report["operations"]["create_user"]["adapter_method_present"])
        self.assertEqual(report["operations"]["create_user"]["status"], "unknown")
        self.assertEqual(report["operations"]["create_user"]["panel_permission"], "unknown")
        self.assertEqual(report["operations"]["create_user"]["live_test"], "not_tested")
        self.assertEqual(report["operations"]["manage_resellers"]["status"], "unsupported")

    async def test_health_and_auth_header(self):
        result=await self.adapter.health_check(self.connection)
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls[0][2], 'Bearer fake-key')
        self.assertNotIn('fake-key', repr(result))

    async def test_create_requires_service_id_and_maps_user(self):
        user=await self.adapter.create_user(self.connection,UserCreate(username='alice',data_limit_bytes=100))
        self.assertEqual(user.username,'alice')
        self.assertEqual(user.subscription_url,'https://rebecca.example/sub/alice/key123')
        with self.assertRaises(PanelError):
            await self.adapter.create_user(PanelConnection(id='r2',tenant_id='t1',name='R',kind='rebecca',base_url='https://r.example',auth_mode='api_key',secret_ref='s'),UserCreate(username='bob'))

    async def test_list_users_maps_subscription_fields(self):
        def handler(method,path,kwargs):
            if path.startswith('/api/users?'):
                return Response(200,{'users':[{'username':'alice','status':'active','links':['https://sub.example/alice']} ]})
            return Response(404,{'detail':'not found'})
        adapter=RebeccaAdapter(secret_resolver=lambda _:'fake-key',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        users=await adapter.list_users(self.connection,limit=10)
        self.assertEqual(len(users),1)
        self.assertEqual(users[0].subscription_url,'https://sub.example/alice')

    async def test_update_delete_and_usage_use_expected_user_endpoints(self):
        def handler(method, path, kwargs):
            self.calls.append((method, path, kwargs.get('headers', {}).get('Authorization'), kwargs.get('json')))
            if method == 'PUT' and path == '/api/v2/users/user-17':
                return Response(200, {'username':'alice','status':'disabled','data_limit':2048})
            if method == 'GET' and path == '/api/user/alice/usage':
                return Response(200, {'used_traffic':42})
            return Response(204)
        adapter=RebeccaAdapter(secret_resolver=lambda _:'fake-key',
            client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        updated=await adapter.update_user(self.connection,'user-17',
            UserUpdate(status='disabled',data_limit_bytes=2048))
        await adapter.delete_user(self.connection,'user-17')
        usage=await adapter.get_usage(self.connection,'alice')
        self.assertEqual(updated.status,'disabled')
        self.assertEqual(self.calls[0][0:2],('PUT','/api/v2/users/user-17'))
        self.assertEqual(self.calls[0][3],{'data_limit':2048,'status':'disabled'})
        self.assertEqual(self.calls[1][0:2],('DELETE','/api/user/user-17'))
        self.assertEqual(self.calls[2][0:2],('GET','/api/user/alice/usage'))
        self.assertEqual(usage['used_traffic'],42)

    async def test_http_error_does_not_expose_body(self):
        adapter=RebeccaAdapter(secret_resolver=lambda _:'x',client_factory=lambda **kw:FakeClient(lambda *args:Response(403,text='secret internal detail'),kw['base_url']))
        with self.assertRaisesRegex(PanelError,'HTTP 403') as ctx:
            await adapter.get_user(self.connection,'alice')
        self.assertNotIn('secret internal detail',str(ctx.exception))

    async def test_get_user_only_maps_lookup_404_to_not_found(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                adapter = RebeccaAdapter(
                    secret_resolver=lambda _: "fake-key",
                    client_factory=lambda **kw: FakeClient(
                        lambda *args: Response(status, {"detail": "private detail"}), kw["base_url"]),
                )
                with self.assertRaises(PanelError) as caught:
                    await adapter.get_user(self.connection, "alice")
                self.assertNotIsInstance(caught.exception, PanelUserNotFound)
                self.assertNotIn("private detail", str(caught.exception))

        adapter = RebeccaAdapter(
            secret_resolver=lambda _: "fake-key",
            client_factory=lambda **kw: FakeClient(
                lambda *args: Response(404, {"detail": "not found"}), kw["base_url"]),
        )
        with self.assertRaises(PanelUserNotFound):
            await adapter.get_user(self.connection, "alice")

    async def test_get_user_rejects_success_response_without_identity(self):
        adapter = RebeccaAdapter(
            secret_resolver=lambda _: "fake-key",
            client_factory=lambda **kw: FakeClient(
                lambda *args: Response(200, {}), kw["base_url"]),
        )
        with self.assertRaisesRegex(PanelError, "omitted user identity"):
            await adapter.get_user(self.connection, "alice")

    async def test_get_user_transport_failure_is_not_not_found(self):
        def fail(*args):
            raise RuntimeError("private transport detail")
        adapter = RebeccaAdapter(
            secret_resolver=lambda _: "fake-key",
            client_factory=lambda **kw: FakeClient(fail, kw["base_url"]),
        )
        with self.assertRaisesRegex(PanelError, "transport failed") as caught:
            await adapter.get_user(self.connection, "alice")
        self.assertNotIsInstance(caught.exception, PanelUserNotFound)
        self.assertNotIn("private transport detail", str(caught.exception))

    async def test_capability_report_invalid_connection_fails_closed_without_secret_resolution(self):
        resolved = []
        adapter = RebeccaAdapter(
            secret_resolver=lambda ref: resolved.append(ref) or "fake-key",
            client_factory=lambda **kw: self.fail("capability report must not create a client"),
        )
        report = await adapter.capability_report(replace(self.connection, verify_tls=False))

        self.assertFalse(report["live_panel_verified"])
        self.assertEqual(report["connection_validation"], "failed")
        self.assertEqual(report["error_type"], "PanelError")
        self.assertEqual(resolved, [])
        self.assertEqual(report["operations"]["create_user"]["status"], "unknown")
        self.assertEqual(report["operations"]["create_user"]["panel_permission"], "unknown")

    async def test_get_user_rejects_boolean_expiry_and_traffic_counters(self):
        invalid_payloads = [
            ({"username": "alice", "used_traffic": True, "data_limit": 10}, "invalid usage counter"),
            ({"username": "alice", "used_traffic": 1, "data_limit": False}, "invalid data limit"),
            ({"username": "alice", "expire": True}, "invalid expiry"),
        ]
        for payload, expected in invalid_payloads:
            with self.subTest(expected=expected, payload=payload):
                adapter = RebeccaAdapter(
                    secret_resolver=lambda _: "fake-key",
                    client_factory=lambda **kw: FakeClient(
                        lambda *args: Response(200, payload), kw["base_url"]),
                )
                with self.assertRaisesRegex(PanelError, expected):
                    await adapter.get_user(self.connection, "alice")

    async def test_get_user_rejects_malformed_or_negative_numeric_fields_safely(self):
        invalid_payloads = [
            ({"username": "alice", "used_traffic": "provider-secret", "data_limit": 10},
             "invalid usage counter"),
            ({"username": "alice", "used_traffic": -1, "data_limit": 10},
             "negative usage counter"),
            ({"username": "alice", "used_traffic": 1, "data_limit": "provider-secret"},
             "invalid data limit"),
            ({"username": "alice", "used_traffic": 1, "data_limit": -10},
             "negative data limit"),
            ({"username": "alice", "expire": "not-a-date"},
             "invalid expiry"),
            ({"username": "alice", "expire": 10**100},
             "invalid expiry"),
        ]
        for payload, expected in invalid_payloads:
            with self.subTest(expected=expected, payload=payload):
                adapter = RebeccaAdapter(
                    secret_resolver=lambda _: "fake-key",
                    client_factory=lambda **kw: FakeClient(
                        lambda *args: Response(200, payload), kw["base_url"]),
                )
                with self.assertRaisesRegex(PanelError, expected) as caught:
                    await adapter.get_user(self.connection, "alice")
                self.assertNotIn("provider-secret", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)

    async def test_delete_rejects_explicit_failure_in_success_status_response(self):
        adapter = RebeccaAdapter(secret_resolver=lambda _: "fake-key",
            client_factory=lambda **kw: FakeClient(
                lambda *args: Response(200, {"success": False, "detail": "provider-secret"}),
                kw["base_url"]))
        with self.assertRaisesRegex(PanelError, "rejected the operation") as caught:
            await adapter.delete_user(self.connection, "user-17")
        self.assertNotIn("provider-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_list_users_rejects_malformed_items_instead_of_silently_dropping_them(self):
        adapter = RebeccaAdapter(secret_resolver=lambda _: "fake-key",
            client_factory=lambda **kw: FakeClient(
                lambda *args: Response(200, {"users": [
                    {"username": "alice", "status": "active"}, "malformed-user-record"
                ]}), kw["base_url"]))
        with self.assertRaisesRegex(PanelError, "user list response was invalid"):
            await adapter.list_users(self.connection)
