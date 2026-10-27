import json
import unittest
import uuid
from manubisguard.panels.adapters.sanaei import SanaeiAdapter
from manubisguard.panels.contracts import PanelConnection, PanelError, PanelUserNotFound, UserCreate, UserUpdate

class Response:
    def __init__(self,status,data=None): self.status_code=status; self._data=data; self.content=json.dumps(data).encode() if data is not None else b''
    def json(self): return self._data

class FakeClient:
    def __init__(self,handler,base_url): self.handler=handler; self.base_url=base_url.rstrip('/'); self.cookies={}
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
    async def request(self,method,path,**kwargs): return self.handler(method,path,kwargs)
    async def post(self,path,**kwargs): return await self.request('POST',path,**kwargs)

class SanaeiAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_report_marks_local_surface_without_claiming_live_permission(self):
        adapter = SanaeiAdapter(secret_resolver=lambda _:'token-secret',
            client_factory=lambda **kw: FakeClient(lambda *args: self.fail('report must not call network'), kw['base_url']))
        conn = PanelConnection(id='s1',tenant_id='t',name='Sanaei',kind='sanaei',
            base_url='https://sanaei.example',auth_mode='access_token',secret_ref='s')
        report = await adapter.capability_report(conn)
        self.assertFalse(report['live_panel_verified'])
        self.assertTrue(report['operations']['create_user']['adapter_method_present'])
        self.assertEqual(report['operations']['create_user']['status'], 'unknown')
        self.assertEqual(report['operations']['create_user']['panel_permission'], 'unknown')
        self.assertEqual(report['operations']['create_user']['live_test'], 'not_tested')
        self.assertEqual(report['operations']['manage_resellers']['status'], 'unsupported')
        capabilities = await adapter.capabilities(conn)
        self.assertFalse(capabilities.subscription_link)

    async def test_token_list_users_maps_clients_and_applies_offset_limit(self):
        calls = []
        def handler(method, path, kwargs):
            calls.append((method, path, kwargs.get('headers', {}).get('Authorization')))
            return Response(200, {'success': True, 'obj': [
                {'email': 'alice', 'id': 'uuid-a', 'totalGB': 2, 'enable': True, 'up': 10, 'down': 20},
                {'email': 'bob', 'id': 'uuid-b', 'totalGB': 3, 'enable': False, 'up': 30, 'down': 40},
                {'email': 'carol', 'id': 'uuid-c', 'totalGB': 4, 'enable': True, 'up': 50, 'down': 60},
            ]})
        adapter = SanaeiAdapter(secret_resolver=lambda _: 'token-secret',
            client_factory=lambda **kw: FakeClient(handler, kw['base_url']))
        conn = PanelConnection(id='s', tenant_id='t', name='Sanaei', kind='sanaei',
            base_url='https://sanaei.example', auth_mode='access_token', secret_ref='s')
        users = await adapter.list_users(conn, limit=1, offset=1)
        self.assertEqual([user.username for user in users], ['bob'])
        self.assertEqual(users[0].status, 'disabled')
        self.assertEqual(users[0].provider_user_id, 'bob')
        self.assertEqual(users[0].data_limit_bytes, 3 * 1024**3)
        self.assertEqual(users[0].used_bytes, 70)
        self.assertEqual(users[0].metadata['uuid'], 'uuid-b')
        self.assertEqual(calls, [('GET', '/panel/api/clients/list', 'Bearer token-secret')])

    async def test_token_list_users_fails_closed_on_non_list_response(self):
        adapter = SanaeiAdapter(secret_resolver=lambda _: 'token-secret',
            client_factory=lambda **kw: FakeClient(
                lambda *args: Response(200, {'success': True, 'obj': {'items': []}}), kw['base_url']))
        conn = PanelConnection(id='s', tenant_id='t', name='Sanaei', kind='sanaei',
            base_url='https://sanaei.example', auth_mode='access_token', secret_ref='s')
        with self.assertRaisesRegex(PanelError, 'clients list did not contain a list'):
            await adapter.list_users(conn)

    async def test_token_create_payload_and_expiry_milliseconds(self):
        seen=[]
        def handler(method,path,kwargs):
            payload=kwargs.get('json')
            seen.append((method,path,kwargs.get('headers',{}).get('Authorization'),payload))
            if path == '/panel/api/clients/add': return Response(200,{'success':True,'obj':{'client':payload['client'],'inboundIds':[3]}})
            return Response(200,{'success':True,'obj':[]})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token-secret',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s1',tenant_id='t',name='Sanaei',kind='sanaei',base_url='https://sanaei.example',auth_mode='access_token',secret_ref='s',metadata={'inbound_ids':[3]})
        user=await adapter.create_user(conn,UserCreate(username='alice',data_limit_bytes=1073741824,extra={'inbound_ids':[3]}))
        self.assertEqual(user.username,'alice')
        self.assertEqual(seen[0][2],'Bearer token-secret')
        self.assertEqual(seen[0][3]['inboundIds'],[3])
        self.assertEqual(seen[0][3]['client']['totalGB'],1)
        self.assertEqual(user.data_limit_bytes,1073741824)
        self.assertNotIn('token-secret',repr(user))

    async def test_legacy_login_then_inbounds_list(self):
        calls=[]
        def handler(method,path,kwargs):
            calls.append((path,method))
            if path == '/login': return Response(200,{'success':True})
            if path == '/panel/api/inbounds/list': return Response(200,{'success':True,'obj':[{'id':7,'settings':json.dumps({'clients':[{'id':'uuid-1','email':'alice','totalGB':100,'expiryTime':0,'enable':True,'up':4,'down':6}]})}]})
            return Response(200,{'success':True,'obj':None})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'password',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s2',tenant_id='t',name='Legacy',kind='sanaei',base_url='https://sanaei.example',auth_mode='username_password',secret_ref='s',metadata={'username':'admin'})
        user=await adapter.get_user(conn,'alice')
        self.assertEqual(user.provider_user_id,'alice')
        self.assertEqual(user.metadata['uuid'],'uuid-1')
        self.assertEqual(user.used_bytes,10)
        self.assertEqual(user.data_limit_bytes,100 * 1024**3)
        self.assertEqual(calls,[('/login','POST'),('/panel/api/inbounds/list','GET')])

    async def test_token_subscription_link_uses_explicit_https_url_not_share_links(self):
        calls=[]
        direct_link='vless://alice@edge.example:443?security=tls'
        def handler(method,path,kwargs):
            calls.append((method,path,kwargs.get('headers',{}).get('Authorization')))
            return Response(200,{'success':True,'obj':{
                'client':{'email':'alice','id':'uuid-1'},
                'links':[direct_link],
                'subscription_url':'https://sub.example/s/alice-token',
            }})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',
            auth_mode='access_token',secret_ref='s')
        link=await adapter.get_subscription_url(conn,'alice')
        self.assertEqual(link,'https://sub.example/s/alice-token')
        self.assertEqual(calls,[('GET','/panel/api/clients/get/alice','Bearer token')])

    async def test_configured_subscription_server_builds_https_url_from_sub_id(self):
        calls=[]
        def handler(method,path,kwargs):
            calls.append((method,path))
            return Response(200,{'success':True,'obj':{
                'client':{'email':'alice','id':'uuid-1','subId':'secret-sub-id'},
                'links':['vless://alice@edge.example:443'],
            }})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',
            client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://panel.example',
            auth_mode='access_token',secret_ref='s',
            metadata={'subscription_base_url':'https://sub.example:2096','subscription_path':'sub'})
        capabilities=await adapter.capabilities(conn)
        self.assertTrue(capabilities.subscription_link)
        link=await adapter.get_subscription_url(conn,'alice')
        self.assertEqual(link,'https://sub.example:2096/sub/secret-sub-id')
        self.assertEqual(calls,[('GET','/panel/api/clients/get/alice')])

    async def test_configured_subscription_server_requires_client_sub_id(self):
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',
            client_factory=lambda **kw:FakeClient(lambda *args:Response(200,{'success':True,
                'obj':{'client':{'email':'alice','id':'uuid-1'},'links':['vless://direct']}}),kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://panel.example',
            auth_mode='access_token',secret_ref='s',
            metadata={'subscription_base_url':'https://sub.example:2096','subscription_path':'sub'})
        with self.assertRaisesRegex(PanelError,'valid subscription ID'):
            await adapter.get_subscription_url(conn,'alice')

    async def test_invalid_subscription_server_config_does_not_enable_capability(self):
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',
            client_factory=lambda **kw:FakeClient(lambda *args: self.fail('invalid config should not be used'),kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://panel.example',
            auth_mode='access_token',secret_ref='s',
            metadata={'subscription_base_url':'https://sub.example:2096/path','subscription_path':'../bad'})
        capabilities=await adapter.capabilities(conn)
        self.assertFalse(capabilities.subscription_link)

    async def test_token_subscription_link_fails_closed_when_only_direct_share_links_exist(self):
        direct_link='vless://alice@edge.example:443?security=tls'
        calls=[]
        def handler(method,path,kwargs):
            calls.append((method,path))
            return Response(200,{'success':True,'obj':{'client':{'email':'alice','id':'uuid-1'},
                'links':[direct_link],'subscription_url':direct_link}})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',
            client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',
            auth_mode='access_token',secret_ref='s')
        with self.assertRaisesRegex(PanelError,'subscription server origin/path is not configured'):
            await adapter.get_subscription_url(conn,'alice')
        self.assertEqual(calls,[('GET','/panel/api/clients/get/alice')])
        self.assertNotIn(direct_link, str(calls))

    async def test_legacy_subscription_link_fails_before_any_request(self):
        adapter=SanaeiAdapter(secret_resolver=lambda _:'password',
            client_factory=lambda **kw:FakeClient(lambda *args: self.fail('unexpected request'),kw['base_url']))
        conn=PanelConnection(id='legacy',tenant_id='t',name='Legacy',kind='sanaei',
            base_url='https://s.example',auth_mode='username_password',secret_ref='s',
            metadata={'username':'admin'})
        with self.assertRaisesRegex(PanelError,'not verified for legacy'):
            await adapter.get_subscription_url(conn,'alice')

    async def test_user_mapping_rejects_boolean_expiry_and_traffic_counters(self):
        invalid_objects = [
            ({"client": {"email": "alice", "totalGB": True}}, "invalid totalGB limit"),
            ({"client": {"email": "alice", "expiryTime": True}}, "invalid expiry"),
            ({"client": {"email": "alice", "up": True, "down": 4}}, "invalid usage counter"),
            ({"client": {"email": "alice"}, "used_traffic": True}, "invalid usage counter"),
        ]
        for obj, expected in invalid_objects:
            with self.subTest(expected=expected, obj=obj):
                with self.assertRaisesRegex(PanelError, expected):
                    SanaeiAdapter._map(obj, "https://panel.example")

    async def test_token_usage_uses_documented_traffic_endpoint(self):
        calls=[]
        def handler(method,path,kwargs):
            calls.append((method,path))
            return Response(200,{'success':True,'obj':{'up':123,'down':456,'total':1000,'totalGB':2}})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',auth_mode='access_token',secret_ref='s')
        usage=await adapter.get_usage(conn,'alice')
        self.assertEqual(usage['used_bytes'],579)
        self.assertEqual(usage['total_bytes'],2 * 1024**3)
        self.assertEqual(calls,[('GET','/panel/api/clients/traffic/alice')])

    async def test_legacy_usage_aggregates_valid_client_stats_across_inbounds(self):
        calls=[]
        def handler(method,path,kwargs):
            calls.append((method,path))
            if path == '/login': return Response(200,{'success':True})
            if path == '/panel/api/inbounds/list':
                return Response(200,{'success':True,'obj':[
                    {'id':7,'clientStats':[{'email':'alice','up':100,'down':200,'total':1000}]},
                    {'id':8,'clientStats':[{'email':'alice','up':50,'down':75,'total':900}]},
                    {'id':9,'clientStats':[{'email':'other','up':999,'down':999,'total':5000}]},
                ]})
            return Response(200,{'success':True,'obj':None})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'password',
            client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='legacy',tenant_id='t',name='Legacy',kind='sanaei',
            base_url='https://s.example',auth_mode='username_password',secret_ref='s',
            metadata={'username':'admin'})
        usage=await adapter.get_usage(conn,'alice')
        self.assertEqual(usage,{'username':'alice','used_bytes':425,'total_bytes':1000})
        self.assertEqual(calls,[('POST','/login'),('GET','/panel/api/inbounds/list')])

    async def test_legacy_usage_rejects_missing_counters_instead_of_billing_zero(self):
        invalid_stats=[
            ({'email':'alice','down':4,'total':10}, 'invalid traffic counters'),
            ({'email':'alice','up':3,'total':10}, 'invalid traffic counters'),
            ({'email':'alice','up':3,'down':4}, 'invalid traffic counters'),
            ({'email':'alice','up':'not-a-number','down':4,'total':10}, 'invalid traffic counters'),
            ({'email':'alice','up':True,'down':4,'total':10}, 'invalid traffic counters'),
            ({'email':'alice','up':-1,'down':4,'total':10}, 'negative traffic counters'),
        ]
        for stat, expected_error in invalid_stats:
            with self.subTest(stat=stat):
                def handler(method,path,kwargs):
                    if path == '/login': return Response(200,{'success':True})
                    if path == '/panel/api/inbounds/list':
                        return Response(200,{'success':True,'obj':[{'id':7,'clientStats':[stat]}]})
                    return Response(200,{'success':True,'obj':None})
                adapter=SanaeiAdapter(secret_resolver=lambda _:'password',
                    client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
                conn=PanelConnection(id='legacy',tenant_id='t',name='Legacy',kind='sanaei',
                    base_url='https://s.example',auth_mode='username_password',secret_ref='s',
                    metadata={'username':'admin'})
                with self.assertRaisesRegex(PanelError,expected_error):
                    await adapter.get_usage(conn,'alice')

    async def test_fractional_gib_limit_fails_closed_before_provider_mutation(self):
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',client_factory=lambda **kw:FakeClient(lambda *args: self.fail('unexpected request'),kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',auth_mode='access_token',secret_ref='s',metadata={'inbound_ids':[3]})
        with self.assertRaisesRegex(PanelError,'whole GiB'):
            await adapter.create_user(conn,UserCreate(username='alice',data_limit_bytes=1024**3+1,extra={'inbound_ids':[3]}))

    async def test_token_update_preserves_identity_and_changes_status_and_limit(self):
        calls=[]
        existing={'id':'uuid-1','email':'alice','totalGB':2,'expiryTime':0,'enable':True,'up':1,'down':2}
        def handler(method,path,kwargs):
            calls.append((method,path,kwargs.get('json')))
            if path == '/panel/api/clients/get/alice':
                return Response(200,{'success':True,'obj':{'client':existing,'inboundIds':[3]}})
            if path == '/panel/api/clients/update/alice':
                return Response(200,{'success':True,'obj':{'client':kwargs['json'],'inboundIds':[3]}})
            return Response(200,{'success':True,'obj':[]})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',
            auth_mode='access_token',secret_ref='s')
        updated=await adapter.update_user(conn,'alice',UserUpdate(status='disabled',data_limit_bytes=3 * 1024**3))
        self.assertEqual(updated.username,'alice')
        self.assertEqual(calls[0][0:2],('GET','/panel/api/clients/get/alice'))
        self.assertEqual(calls[1][0:2],('POST','/panel/api/clients/update/alice'))
        self.assertEqual(calls[1][2]['totalGB'],3)
        self.assertFalse(calls[1][2]['enable'])
        self.assertEqual(calls[1][2]['id'],'uuid-1')

    async def test_token_update_fails_closed_when_provider_returns_http_error(self):
        calls = []
        existing = {'id': 'uuid-1', 'email': 'alice', 'totalGB': 2,
                    'expiryTime': 0, 'enable': True, 'up': 1, 'down': 2}

        def handler(method, path, kwargs):
            calls.append((method, path))
            if path == '/panel/api/clients/get/alice':
                return Response(200, {'success': True, 'obj': {'client': existing, 'inboundIds': [3]}})
            if path == '/panel/api/clients/update/alice':
                return Response(503, {'success': False})
            self.fail(f'unexpected request: {method} {path}')

        adapter = SanaeiAdapter(secret_resolver=lambda _: 'token-secret',
            client_factory=lambda **kw: FakeClient(handler, kw['base_url']))
        conn = PanelConnection(id='s', tenant_id='t', name='Sanaei', kind='sanaei',
            base_url='https://sanaei.example', auth_mode='access_token', secret_ref='s')

        with self.assertRaisesRegex(PanelError, 'Sanaei returned HTTP 503'):
            await adapter.update_user(conn, 'alice', UserUpdate(status='disabled'))

        self.assertEqual(calls, [
            ('GET', '/panel/api/clients/get/alice'),
            ('POST', '/panel/api/clients/update/alice'),
        ])

    async def test_legacy_create_and_list_use_single_inbound_contract(self):
        calls=[]
        listed={'id':'uuid-existing','email':'existing','totalGB':2,'expiryTime':0,'enable':True,'up':3,'down':4}
        def handler(method,path,kwargs):
            calls.append((method,path,kwargs.get('json'),kwargs.get('data')))
            if path == '/login': return Response(200,{'success':True})
            if path == '/panel/api/inbounds/get/7':
                return Response(200,{'success':True,'obj':{'id':7,'protocol':'vless','settings':{'clients':[]}}})
            if path == '/panel/api/inbounds/addClient':
                body=json.loads(kwargs['json']['settings'])
                self.assertEqual(kwargs['json']['id'],7)
                self.assertEqual(body['clients'][0]['email'],'new-user')
                return Response(200,{'success':True,'obj':None})
            if path == '/panel/api/inbounds/list':
                return Response(200,{'success':True,'obj':[{'id':7,'protocol':'vless',
                    'settings':json.dumps({'clients':[listed]})}]})
            return Response(200,{'success':True,'obj':None})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'password',
            client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='legacy',tenant_id='t',name='Legacy',kind='sanaei',
            base_url='https://s.example',auth_mode='username_password',secret_ref='s',
            metadata={'username':'admin','inbound_ids':[7]})
        created=await adapter.create_user(conn,UserCreate(username='new-user',data_limit_bytes=2 * 1024**3,
            extra={'inbound_ids':[7]}))
        users=await adapter.list_users(conn,limit=999,offset=0)
        self.assertEqual(created.username,'new-user')
        uuid.UUID(created.metadata['uuid'])
        self.assertIn(('GET','/panel/api/inbounds/get/7'),[(m,p) for m,p,_,_ in calls])
        self.assertIn(('POST','/panel/api/inbounds/addClient'),[(m,p) for m,p,_,_ in calls])
        self.assertEqual([user.username for user in users],['existing'])
        self.assertIn(('GET','/panel/api/inbounds/list'),[(m,p) for m,p,_,_ in calls])

    async def test_legacy_create_fails_closed_for_unmapped_protocol_credentials(self):
        calls=[]
        def handler(method,path,kwargs):
            calls.append((method,path))
            if path == '/login': return Response(200,{'success':True})
            if path == '/panel/api/inbounds/get/7':
                return Response(200,{'success':True,'obj':{'id':7,'protocol':'trojan','settings':{'clients':[]}}})
            return Response(200,{'success':True,'obj':None})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'password',
            client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='legacy',tenant_id='t',name='Legacy',kind='sanaei',
            base_url='https://s.example',auth_mode='username_password',secret_ref='s',
            metadata={'username':'admin','inbound_ids':[7]})
        with self.assertRaisesRegex(PanelError,'VLESS/VMess'):
            await adapter.create_user(conn,UserCreate(username='new-user',data_limit_bytes=2 * 1024**3,
                extra={'inbound_ids':[7]}))
        self.assertNotIn(('POST','/panel/api/inbounds/addClient'),calls)

    async def test_legacy_create_rejects_invalid_supplied_uuid_before_mutation(self):
        calls=[]
        def handler(method,path,kwargs):
            calls.append((method,path))
            if path == '/login': return Response(200,{'success':True})
            if path == '/panel/api/inbounds/get/7':
                return Response(200,{'success':True,'obj':{'id':7,'protocol':'vless','settings':{'clients':[]}}})
            return Response(200,{'success':True,'obj':None})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'password',
            client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='legacy',tenant_id='t',name='Legacy',kind='sanaei',
            base_url='https://s.example',auth_mode='username_password',secret_ref='s',
            metadata={'username':'admin','inbound_ids':[7]})
        with self.assertRaisesRegex(PanelError,'valid UUID'):
            await adapter.create_user(conn,UserCreate(username='new-user',data_limit_bytes=2 * 1024**3,
                extra={'inbound_ids':[7],'uuid':'not-a-uuid'}))
        self.assertNotIn(('POST','/panel/api/inbounds/addClient'),calls)

    async def test_legacy_update_and_delete_choose_protocol_specific_client_id(self):
        for protocol, expected_id in (
            ('vless','uuid-1'), ('vmess','uuid-1'),
            ('trojan','trojan-password'), ('shadowsocks','alice'),
        ):
            with self.subTest(protocol=protocol):
                calls=[]
                client={'id':'uuid-1','password':'trojan-password','email':'alice',
                    'totalGB':2,'expiryTime':0,'enable':True,'up':1,'down':2}
                def handler(method,path,kwargs):
                    calls.append((method,path,kwargs.get('json')))
                    if path == '/login': return Response(200,{'success':True})
                    if path == '/panel/api/inbounds/list':
                        return Response(200,{'success':True,'obj':[{'id':7,'protocol':protocol,
                            'settings':json.dumps({'clients':[client]})}]})
                    return Response(200,{'success':True,'obj':None})
                adapter=SanaeiAdapter(secret_resolver=lambda _:'password',
                    client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
                conn=PanelConnection(id='legacy',tenant_id='t',name='Legacy',kind='sanaei',
                    base_url='https://s.example',auth_mode='username_password',secret_ref='s',
                    metadata={'username':'admin','inbound_ids':[7]})
                await adapter.update_user(conn,'alice',UserUpdate(status='disabled'))
                await adapter.delete_user(conn,'alice')
                paths=[path for _,path,_ in calls]
                self.assertIn(f'/panel/api/inbounds/updateClient/{expected_id}',paths)
                self.assertIn(f'/panel/api/inbounds/7/delClient/{expected_id}',paths)

    async def test_legacy_list_users_merges_same_client_across_inbounds(self):
        inbound_a = {"id": 7, "protocol": "vless", "settings": json.dumps({"clients": [
            {"id": "uuid-1", "email": "alice", "totalGB": 2, "expiryTime": 0,
             "enable": True, "up": 4, "down": 6}
        ]})}
        inbound_b = {"id": "8", "protocol": "vless", "settings": json.dumps({"clients": [
            {"id": "uuid-1", "email": "alice", "totalGB": 2, "expiryTime": 0,
             "enable": True, "up": 9, "down": 11}
        ]})}
        def handler(method, path, kwargs):
            if path == "/login": return Response(200, {"success": True})
            if path == "/panel/api/inbounds/list": return Response(200, {"success": True, "obj": [inbound_a, inbound_b]})
            self.fail(f"unexpected request: {method} {path}")
        adapter = SanaeiAdapter(secret_resolver=lambda _: "password",
            client_factory=lambda **kw: FakeClient(handler, kw["base_url"]))
        conn = PanelConnection(id="legacy", tenant_id="t", name="Legacy", kind="sanaei",
            base_url="https://sanaei.example", auth_mode="username_password", secret_ref="s",
            metadata={"username": "admin"})
        users = await adapter.list_users(conn)
        self.assertEqual([user.username for user in users], ["alice"])
        self.assertEqual(users[0].metadata["inbound_ids"], [7, 8])

    async def test_legacy_multi_inbound_user_update_and_delete_fail_before_mutation(self):
        inbound_a = {"id": 7, "protocol": "vless", "settings": json.dumps({"clients": [
            {"id": "uuid-1", "email": "alice", "totalGB": 2, "expiryTime": 0, "enable": True}
        ]})}
        inbound_b = {"id": 8, "protocol": "vless", "settings": json.dumps({"clients": [
            {"id": "uuid-1", "email": "alice", "totalGB": 2, "expiryTime": 0, "enable": True}
        ]})}
        calls = []
        def handler(method, path, kwargs):
            calls.append((method, path))
            if path == "/login": return Response(200, {"success": True})
            if path == "/panel/api/inbounds/list": return Response(200, {"success": True, "obj": [inbound_a, inbound_b]})
            self.fail(f"mutation must not be sent for multi-inbound identity: {method} {path}")
        adapter = SanaeiAdapter(secret_resolver=lambda _: "password",
            client_factory=lambda **kw: FakeClient(handler, kw["base_url"]))
        conn = PanelConnection(id="legacy", tenant_id="t", name="Legacy", kind="sanaei",
            base_url="https://sanaei.example", auth_mode="username_password", secret_ref="s",
            metadata={"username": "admin"})
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(PanelError, "exactly one inbound"):
                    if operation == "update":
                        await adapter.update_user(conn, "alice", UserUpdate(status="disabled"))
                    else:
                        await adapter.delete_user(conn, "alice")
        self.assertFalse(any("updateClient" in path or "delClient" in path for _, path in calls))

    async def test_legacy_list_users_rejects_conflicting_identity_for_duplicate_email(self):
        inbound_a = {"id": 7, "protocol": "vless", "settings": json.dumps({"clients": [
            {"id": "uuid-1", "email": "alice", "totalGB": 2, "enable": True}
        ]})}
        inbound_b = {"id": 8, "protocol": "vless", "settings": json.dumps({"clients": [
            {"id": "uuid-different", "email": "alice", "totalGB": 2, "enable": True}
        ]})}
        def handler(method, path, kwargs):
            if path == "/login": return Response(200, {"success": True})
            if path == "/panel/api/inbounds/list": return Response(200, {"success": True, "obj": [inbound_a, inbound_b]})
            self.fail(f"unexpected request: {method} {path}")
        adapter = SanaeiAdapter(secret_resolver=lambda _: "password",
            client_factory=lambda **kw: FakeClient(handler, kw["base_url"]))
        conn = PanelConnection(id="legacy", tenant_id="t", name="Legacy", kind="sanaei",
            base_url="https://sanaei.example", auth_mode="username_password", secret_ref="s",
            metadata={"username": "admin"})
        with self.assertRaisesRegex(PanelError, "ambiguous.*inbounds"):
            await adapter.list_users(conn)

    async def test_token_delete_uses_documented_endpoint(self):
        calls=[]
        def handler(method,path,kwargs): calls.append((method,path)); return Response(200,{'success':True,'obj':None})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',auth_mode='access_token',secret_ref='s')
        caps=await adapter.capabilities(conn)
        self.assertTrue(caps.delete_user)
        await adapter.delete_user(conn,'alice')
        self.assertEqual(calls,[('POST','/panel/api/clients/del/alice')])

    async def test_token_get_user_only_maps_lookup_404_to_not_found(self):
        conn = PanelConnection(id="s", tenant_id="t", name="Sanaei", kind="sanaei",
            base_url="https://sanaei.example", auth_mode="access_token", secret_ref="s")

        for status in (401, 403, 500):
            with self.subTest(status=status):
                adapter = SanaeiAdapter(secret_resolver=lambda _: "token-secret",
                    client_factory=lambda **kw: FakeClient(
                        lambda *args: Response(status, {"success": False}), kw["base_url"]))
                with self.assertRaises(PanelError) as caught:
                    await adapter.get_user(conn, "alice")
                self.assertNotIsInstance(caught.exception, PanelUserNotFound)

        adapter = SanaeiAdapter(secret_resolver=lambda _: "token-secret",
            client_factory=lambda **kw: FakeClient(
                lambda *args: Response(404, {"success": False}), kw["base_url"]))
        with self.assertRaises(PanelUserNotFound):
            await adapter.get_user(conn, "alice")

    async def test_token_get_user_rejects_success_response_without_client_identity(self):
        adapter = SanaeiAdapter(secret_resolver=lambda _: "token-secret",
            client_factory=lambda **kw: FakeClient(
                lambda *args: Response(200, {"success": True, "obj": {"client": {"id": "uuid-only"}}}),
                kw["base_url"]))
        conn = PanelConnection(id="s", tenant_id="t", name="Sanaei", kind="sanaei",
            base_url="https://sanaei.example", auth_mode="access_token", secret_ref="s")
        with self.assertRaisesRegex(PanelError, "omitted client email"):
            await adapter.get_user(conn, "alice")

    async def test_token_get_user_transport_failure_is_not_not_found(self):
        def fail(*args):
            raise RuntimeError("private transport detail")
        adapter = SanaeiAdapter(secret_resolver=lambda _: "token-secret",
            client_factory=lambda **kw: FakeClient(fail, kw["base_url"]))
        conn = PanelConnection(id="s", tenant_id="t", name="Sanaei", kind="sanaei",
            base_url="https://sanaei.example", auth_mode="access_token", secret_ref="s")
        with self.assertRaisesRegex(PanelError, "transport failed") as caught:
            await adapter.get_user(conn, "alice")
        self.assertNotIsInstance(caught.exception, PanelUserNotFound)
        self.assertNotIn("private transport detail", str(caught.exception))

    async def test_capability_report_invalid_connection_fails_closed_without_secret_resolution(self):
        resolved = []
        adapter = SanaeiAdapter(
            secret_resolver=lambda ref: resolved.append(ref) or "token-secret",
            client_factory=lambda **kw: self.fail("capability report must not create a client"),
        )
        conn = PanelConnection(id="s", tenant_id="t", name="Sanaei", kind="sanaei",
            base_url="https://sanaei.example", auth_mode="access_token", secret_ref="s",
            verify_tls=False)
        report = await adapter.capability_report(conn)

        self.assertFalse(report["live_panel_verified"])
        self.assertEqual(report["connection_validation"], "failed")
        self.assertEqual(report["error_type"], "PanelError")
        self.assertEqual(resolved, [])
        self.assertEqual(report["operations"]["create_user"]["status"], "unknown")
        self.assertEqual(report["operations"]["create_user"]["panel_permission"], "unknown")

    async def test_legacy_update_and_delete_http_errors_are_generic_and_redacted(self):
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                calls = []
                inbound = {"id": 7, "protocol": "vless", "settings": json.dumps({"clients": [
                    {"id": "uuid-1", "email": "alice", "totalGB": 2, "expiryTime": 0,
                     "enable": True, "up": 1, "down": 2}
                ]})}

                def handler(method, path, kwargs):
                    calls.append((method, path))
                    if path == "/login":
                        return Response(200, {"success": True})
                    if path == "/panel/api/inbounds/list":
                        return Response(200, {"success": True, "obj": [inbound]})
                    if "/updateClient/" in path or "/delClient/" in path:
                        return Response(503, {"detail": "provider-secret-response"})
                    self.fail(f"unexpected request: {method} {path}")

                adapter = SanaeiAdapter(secret_resolver=lambda _: "password-secret",
                    client_factory=lambda **kw: FakeClient(handler, kw["base_url"]))
                conn = PanelConnection(id="legacy", tenant_id="t", name="Legacy", kind="sanaei",
                    base_url="https://sanaei.example", auth_mode="username_password", secret_ref="s",
                    metadata={"username": "admin"})
                with self.assertRaisesRegex(PanelError, "HTTP 503") as caught:
                    if operation == "update":
                        await adapter.update_user(conn, "alice", UserUpdate(status="disabled"))
                    else:
                        await adapter.delete_user(conn, "alice")
                self.assertNotIsInstance(caught.exception, PanelUserNotFound)
                self.assertNotIn("provider-secret-response", str(caught.exception))
                self.assertTrue(any("/updateClient/" in path or "/delClient/" in path
                                    for _, path in calls))

    async def test_legacy_update_and_delete_transport_errors_are_not_not_found(self):
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                calls = []
                inbound = {"id": 7, "protocol": "vless", "settings": json.dumps({"clients": [
                    {"id": "uuid-1", "email": "alice", "totalGB": 2, "expiryTime": 0,
                     "enable": True, "up": 1, "down": 2}
                ]})}

                def handler(method, path, kwargs):
                    calls.append((method, path))
                    if path == "/login":
                        return Response(200, {"success": True})
                    if path == "/panel/api/inbounds/list":
                        return Response(200, {"success": True, "obj": [inbound]})
                    if "/updateClient/" in path or "/delClient/" in path:
                        raise RuntimeError("provider-secret-transport-detail")
                    self.fail(f"unexpected request: {method} {path}")

                adapter = SanaeiAdapter(secret_resolver=lambda _: "password-secret",
                    client_factory=lambda **kw: FakeClient(handler, kw["base_url"]))
                conn = PanelConnection(id="legacy", tenant_id="t", name="Legacy", kind="sanaei",
                    base_url="https://sanaei.example", auth_mode="username_password", secret_ref="s",
                    metadata={"username": "admin"})
                with self.assertRaisesRegex(PanelError, "transport failed") as caught:
                    if operation == "update":
                        await adapter.update_user(conn, "alice", UserUpdate(status="disabled"))
                    else:
                        await adapter.delete_user(conn, "alice")
                self.assertNotIsInstance(caught.exception, PanelUserNotFound)
                self.assertNotIn("provider-secret-transport-detail", str(caught.exception))
                self.assertTrue(any("/updateClient/" in path or "/delClient/" in path
                                    for _, path in calls))

    async def test_legacy_update_and_delete_missing_user_fail_before_mutation(self):
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                calls = []

                def handler(method, path, kwargs):
                    calls.append((method, path))
                    if path == "/login":
                        return Response(200, {"success": True})
                    if path == "/panel/api/inbounds/list":
                        return Response(200, {"success": True, "obj": []})
                    self.fail(f"mutation must not run for absent user: {method} {path}")

                adapter = SanaeiAdapter(secret_resolver=lambda _: "password-secret",
                    client_factory=lambda **kw: FakeClient(handler, kw["base_url"]))
                conn = PanelConnection(id="legacy", tenant_id="t", name="Legacy", kind="sanaei",
                    base_url="https://sanaei.example", auth_mode="username_password", secret_ref="s",
                    metadata={"username": "admin"})
                with self.assertRaises(PanelUserNotFound):
                    if operation == "update":
                        await adapter.update_user(conn, "alice", UserUpdate(status="disabled"))
                    else:
                        await adapter.delete_user(conn, "alice")
                self.assertEqual(calls, [("POST", "/login"), ("GET", "/panel/api/inbounds/list")])
