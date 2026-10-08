"""Credential routing tests: no Google calls and no real credentials."""
import asyncio
import unittest
from unittest.mock import patch
from src.credential_manager import CredentialManager
from src.multiuser import current_owner


class FakeStorage:
    def __init__(self):
        self.data = {name: {'token': name} for name in ('admin', 'alice', 'bob')}
        self.states = {name: {} for name in self.data}
        self.reads = []

    async def list_credentials(self, mode):
        return list(self.data)

    async def get_credential_state(self, name, mode):
        return self.states[name]

    async def get_credential(self, name, mode):
        self.reads.append(name)
        return dict(self.data[name])


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = CredentialManager()
        self.storage = FakeStorage()
        self.manager._storage_adapter = self.storage
        self.manager._initialized = True
        self.marker = current_owner.set(None)
        self.patch = patch('src.multiuser.owned_filenames', side_effect=lambda owner, mode, names: [name for name in names if {'admin': '__admin__', 'alice': 'A', 'bob': 'B'}[name] == owner])
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        current_owner.reset(self.marker)

    async def choose(self):
        return await self.manager._get_owned_available_credential('antigravity', 'model')

    async def test_admin_only_legacy(self):
        self.assertEqual((await self.choose())[0], 'admin')

    async def test_user_only_own(self):
        current_owner.set('B')
        for _ in range(5):
            self.assertEqual((await self.choose())[0], 'bob')
        self.assertEqual(set(self.storage.reads), {'bob'})

    async def test_empty_disabled_and_cooldown_never_fallback(self):
        current_owner.set('unknown')
        self.assertIsNone(await self.choose())
        current_owner.set('B')
        self.storage.states['bob'] = {'disabled': True}
        self.assertIsNone(await self.choose())
        self.storage.states['bob'] = {'model_cooldowns': {'model': 99999999999}}
        self.assertIsNone(await self.choose())
        self.assertEqual(self.storage.reads, [])

    async def test_prewarm_task_inherits_identity(self):
        current_owner.set('B')
        task = asyncio.create_task(self.choose())
        current_owner.set('A')
        self.assertEqual((await task)[0], 'bob')
        self.assertEqual((await self.choose())[0], 'alice')

    async def test_concurrent_accounts(self):
        async def request(owner, filename):
            marker = current_owner.set(owner)
            try:
                for _ in range(10):
                    await asyncio.sleep(0)
                    self.assertEqual((await self.choose())[0], filename)
            finally:
                current_owner.reset(marker)
        await asyncio.gather(request('A', 'alice'), request('B', 'bob'))

    async def test_preview_and_credit_state_preserved(self):
        current_owner.set('B')
        self.storage.states['bob'] = {'preview': False, 'enable_credit': True}
        self.assertIsNone(await self.manager._get_owned_available_credential('geminicli', 'x-preview'))
        selected = await self.choose()
        self.assertTrue(selected[1]['enable_credit'])


class HTTPContextTests(unittest.TestCase):
    def test_stream_and_background_context_at_http_boundary(self):
        from fastapi import FastAPI, Depends
        from fastapi.responses import StreamingResponse
        from fastapi.testclient import TestClient
        from src.utils import authenticate_flexible

        app = FastAPI()

        @app.middleware('http')
        async def isolate(request, call_next):
            marker = current_owner.set(None)
            try:
                return await call_next(request)
            finally:
                current_owner.reset(marker)

        @app.get('/stream')
        async def stream(token=Depends(authenticate_flexible)):
            async def chunks():
                for _ in range(2):
                    await asyncio.sleep(0)
                    async def prewarm():
                        return current_owner.get()
                    yield str(await asyncio.create_task(prewarm())) + '\\n'
            return StreamingResponse(chunks())

        async def fake_auth(token):
            current_owner.set({'key-A': 'A', 'key-B': 'B'}[token])

        with patch('src.multiuser.authenticate_user_api', side_effect=fake_auth):
            with TestClient(app) as client:
                for token, owner in [('key-A', 'A'), ('key-B', 'B'), ('key-A', 'A')]:
                    response = client.get('/stream', headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text, (owner + '\\n') * 2)


if __name__ == '__main__':
    unittest.main()
