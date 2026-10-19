import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch
from manubisguard.customer_store import CustomerDataStore
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.providers import PasarGuardResellerProvider
from manubisguard.panels.contracts import PanelError

class Payload:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
    def __repr__(self): return f'Payload({self.__dict__!r})'

class FakeSDK:
    last=None
    instances=[]
    def __init__(self, **kwargs): self.kwargs=kwargs; self.calls=[]; FakeSDK.last=self; FakeSDK.instances.append(self)
    async def get_admins(self, **kwargs):
        self.calls.append(('get_admins',kwargs))
        return SimpleNamespace(admins=[])
    async def get_roles_simple(self, **kwargs):
        self.calls.append(('get_roles_simple',kwargs))
        return SimpleNamespace(roles=[SimpleNamespace(id=9, name='Reseller', is_owner=False), SimpleNamespace(id=10, name='Owner', is_owner=True)])
    async def create_admin(self, **kwargs):
        self.calls.append(('create_admin',kwargs))
        return SimpleNamespace(id=71,username=kwargs['admin'].username)
    async def remove_admin_by_id(self, **kwargs): self.calls.append(('remove_admin_by_id',kwargs))
    async def modify_admin_by_id(self, **kwargs): self.calls.append(('modify_admin_by_id',kwargs))

class PasarGuardResellerProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeSDK.instances.clear(); FakeSDK.last=None
        self.tmp=tempfile.TemporaryDirectory(); self.store=CustomerDataStore(str(Path(self.tmp.name)/'db.sqlite3'),tenant_id='t')
        self.store.register_panel_connection(panel_id='pg',label='PG',provider='pasarguard',base_url='https://pg.example',auth_mode='api_key',secret_ref='env:PG_KEY',enabled=True)
        self.module=types.ModuleType('pasarguard')
        self.module.AdminCreate=Payload; self.module.RoleLimits=Payload; self.module.AdminModify=Payload
        self.patch=patch.dict(sys.modules,{'pasarguard':self.module}); self.patch.start()
        self.provider=PasarGuardResellerProvider(self.store,secret_resolver=lambda _:'secret-token',sdk_factory=FakeSDK)
    def tearDown(self): self.patch.stop(); self.tmp.cleanup()

    async def test_native_admin_create_uses_role_and_one_time_password_not_repr(self):
        plan=ResellerPlan(plan_id='p',panel_id='pg',pricing_mode=PricingMode.FIXED,price=Decimal(100),
                          data_limit_bytes=1024,max_users=5,provider_role_id=9,
                          permission_overrides=(('max_users',5),),currency='IRR')
        created=await self.provider.create_reseller(panel_id='pg',username='reseller1',plan=plan,owner_id='123',volume_gb=None)
        self.assertEqual(created.provider_account_id,'71')
        self.assertEqual(created.username,'reseller1')
        self.assertNotIn(created.initial_password,repr(created))
        call=next(c for c in FakeSDK.last.calls if c[0]=='create_admin')
        payload=call[1]['admin']
        self.assertEqual(payload.role_id,9)
        self.assertEqual(payload.note,'123')
        self.assertEqual(payload.data_limit,1024)

    async def test_validate_plan_requires_role_to_exist_on_connected_panel(self):
        valid = ResellerPlan(plan_id='p', panel_id='pg', pricing_mode=PricingMode.FIXED,
            price=Decimal(100), provider_role_id=9, currency='IRR')
        await self.provider.validate_plan(panel_id='pg', username='reseller1', plan=valid)
        self.assertTrue(any(call[0] == 'get_roles_simple' for instance in FakeSDK.instances for call in instance.calls))

        invalid = ResellerPlan(plan_id='p2', panel_id='pg', pricing_mode=PricingMode.FIXED,
            price=Decimal(100), provider_role_id=999, currency='IRR')
        with self.assertRaisesRegex(PanelError, 'role ID was not found'):
            await self.provider.validate_plan(panel_id='pg', username='reseller2', plan=invalid)

        malformed = ResellerPlan(plan_id='p3', panel_id='pg', pricing_mode=PricingMode.FIXED,
            price=Decimal(100), provider_role_id='not-a-number', currency='IRR')
        with self.assertRaisesRegex(PanelError, 'valid PasarGuard provider role ID'):
            await self.provider.validate_plan(panel_id='pg', username='reseller3', plan=malformed)

    async def test_owner_role_is_rejected_before_reseller_creation(self):
        plan = ResellerPlan(plan_id='owner-role', panel_id='pg', pricing_mode=PricingMode.FIXED,
            price=Decimal(100), provider_role_id=10, currency='IRR')
        with self.assertRaisesRegex(PanelError, 'owner roles cannot be assigned'):
            await self.provider.validate_plan(panel_id='pg', username='reseller1', plan=plan)
        with self.assertRaisesRegex(PanelError, 'owner roles cannot be assigned'):
            await self.provider.create_reseller(panel_id='pg', username='reseller1', plan=plan,
                owner_id='owner', volume_gb=None)
        self.assertFalse(any(call[0] == 'create_admin' for instance in FakeSDK.instances for call in instance.calls))

    async def test_username_lookup_and_status_operations_use_sdk(self):
        self.assertFalse(await self.provider.username_exists(panel_id='pg',username='reseller1'))
        await self.provider.set_reseller_status(panel_id='pg',provider_account_id='71',enabled=False)
        all_calls=[call for instance in FakeSDK.instances for call in instance.calls]
        self.assertTrue(any(call[0]=='get_admins' for call in all_calls))
        self.assertTrue(any(call[0]=='modify_admin_by_id' for call in all_calls))
        await self.provider.delete_reseller(panel_id='pg',provider_account_id='71')
        self.assertTrue(any(call[0]=='remove_admin_by_id' for instance in FakeSDK.instances for call in instance.calls))

    async def test_malformed_admin_id_is_rejected_before_remote_mutation(self):
        for operation in (
            lambda: self.provider.delete_reseller(panel_id='pg', provider_account_id='bad-id'),
            lambda: self.provider.set_reseller_status(panel_id='pg', provider_account_id='0', enabled=False),
            lambda: self.provider.get_reseller(panel_id='pg', provider_account_id='-7'),
        ):
            with self.assertRaisesRegex(PanelError, 'Invalid PasarGuard provider admin ID'):
                await operation()
        self.assertEqual(FakeSDK.instances, [])

    async def test_username_exists_handles_dict_response_case_insensitively(self):
        class DictClient(FakeSDK):
            async def get_admins(self, **kwargs):
                self.calls.append(('get_admins', kwargs))
                return {'admins': [{'id': 3, 'username': 'ResellerOne'}]}
        provider = PasarGuardResellerProvider(
            self.store, secret_resolver=lambda _: 'secret-token', sdk_factory=DictClient)
        self.assertTrue(await provider.username_exists(panel_id='pg', username='resellerone'))

    async def test_username_exists_fails_closed_on_incompatible_response(self):
        class BrokenShapeClient(FakeSDK):
            async def get_admins(self, **kwargs):
                return {'unexpected': []}
        provider = PasarGuardResellerProvider(
            self.store, secret_resolver=lambda _: 'secret-token', sdk_factory=BrokenShapeClient)
        with self.assertRaisesRegex(PanelError, 'incompatible response'):
            await provider.username_exists(panel_id='pg', username='resellerone')

if __name__ == '__main__': unittest.main()
