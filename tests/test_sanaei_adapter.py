import json
import unittest
from manubisguard.panels.adapters.sanaei import SanaeiAdapter
from manubisguard.panels.contracts import PanelConnection, PanelError, UserCreate

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

    async def test_fractional_gib_limit_fails_closed_before_provider_mutation(self):
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',client_factory=lambda **kw:FakeClient(lambda *args: self.fail('unexpected request'),kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',auth_mode='access_token',secret_ref='s',metadata={'inbound_ids':[3]})
        with self.assertRaisesRegex(PanelError,'whole GiB'):
            await adapter.create_user(conn,UserCreate(username='alice',data_limit_bytes=1024**3+1,extra={'inbound_ids':[3]}))

    async def test_token_delete_uses_documented_endpoint(self):
        calls=[]
        def handler(method,path,kwargs): calls.append((method,path)); return Response(200,{'success':True,'obj':None})
        adapter=SanaeiAdapter(secret_resolver=lambda _:'token',client_factory=lambda **kw:FakeClient(handler,kw['base_url']))
        conn=PanelConnection(id='s',tenant_id='t',name='S',kind='sanaei',base_url='https://s.example',auth_mode='access_token',secret_ref='s')
        caps=await adapter.capabilities(conn)
        self.assertTrue(caps.delete_user)
        await adapter.delete_user(conn,'alice')
        self.assertEqual(calls,[('POST','/panel/api/clients/del/alice')])
