"""ASGI identity lifetime regressions (selector coverage is in test_multiuser_agy).

Replaces tests for the removed CredentialManager class and GeminiCLI preview/
credit flags. No compatibility shim for that deleted upstream API is required.
"""
import asyncio
import unittest

from no_network import forbid_network
from src.multiuser.accounts import ADMIN_OWNER, ORPHAN_OWNER, current_owner
from src.multiuser.isolation import OwnerContextMiddleware


class ASGIOwnerLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        forbid_network(self)
        token = current_owner.set(ADMIN_OWNER)
        self.addCleanup(current_owner.reset, token)

    async def test_http_stream_send_and_child_task_keep_identity_until_final_body(self):
        sent = []
        owners = []

        async def send(message):
            sent.append(message)
            owners.append(current_owner.get())

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def app(scope, receive, send):
            self.assertEqual(current_owner.get(), ORPHAN_OWNER)
            current_owner.set('member-A')
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            for more in (True, False):
                async def prewarm():
                    await asyncio.sleep(0)
                    return current_owner.get()
                self.assertEqual(await asyncio.create_task(prewarm()), 'member-A')
                await send({'type': 'http.response.body', 'body': b'x', 'more_body': more})

        await OwnerContextMiddleware(app)({'type': 'http'}, receive, send)
        self.assertEqual(owners, ['member-A'] * 3)
        self.assertFalse(sent[-1]['more_body'])
        self.assertEqual(current_owner.get(), ADMIN_OWNER)

    async def test_exception_after_stream_start_restores_callers_identity(self):
        async def app(scope, receive, send):
            self.assertEqual(current_owner.get(), ORPHAN_OWNER)
            current_owner.set('member-A')
            raise RuntimeError('synthetic disconnected stream')
        with self.assertRaisesRegex(RuntimeError, 'synthetic disconnected'):
            await OwnerContextMiddleware(app)({'type': 'http'}, None, None)
        self.assertEqual(current_owner.get(), ADMIN_OWNER)

    async def test_websocket_defaults_fail_closed_and_lifespan_does_not_change_owner(self):
        seen = []
        async def app(scope, receive, send):
            seen.append((scope['type'], current_owner.get()))
        middleware = OwnerContextMiddleware(app)
        await middleware({'type': 'websocket'}, None, None)
        await middleware({'type': 'lifespan'}, None, None)
        self.assertEqual(seen, [('websocket', ORPHAN_OWNER), ('lifespan', ADMIN_OWNER)])
        self.assertEqual(current_owner.get(), ADMIN_OWNER)


if __name__ == '__main__':
    unittest.main()
