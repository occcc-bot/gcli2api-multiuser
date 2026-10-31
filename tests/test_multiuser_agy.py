"""Email-backed AGY regressions: real temporary SQLite, no Google/network calls.

Only configuration/transport and OAuth provider boundaries are mocked. The
account repository, email database, selector, cache, auth, HTTP middleware and
upstream retry/prewarm code under test are the real implementations.
"""
import asyncio
from collections import defaultdict
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI, Response
from fastapi.responses import StreamingResponse

from no_network import forbid_network
from src import credential_manager as manager, session_cache
from src.api import antigravity
from src.multiuser import accounts, isolation, storage
from src.storage.database import Database
from src.utils import authenticate_flexible
from test_multiuser import make_user

MODEL = 'claude-synthetic'
PROMPT = {'contents': [{'role': 'user', 'parts': [{'text': 'identical prompt'}]}]}


class FakeRedis:
    """Enough of redis.asyncio for the real session cache; never opens sockets."""
    def __init__(self):
        self.data = {}

    async def get(self, key):
        value = self.data.get(key)
        return value.encode() if value else None

    async def setex(self, key, ttl, value):
        self.data[key] = value

    async def expire(self, key, ttl):
        return key in self.data

    async def delete(self, key):
        return self.data.pop(key, None) is not None


class EmailIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        forbid_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            'CREDENTIALS_DIR': temp.name,
            'MULTIUSER_DB_PATH': str(Path(temp.name) / 'members.sqlite3'),
            'POSTGRESQL_URI': '',
            'MULTIUSER_ALLOW_LEGACY_API_PASSWORD': 'true',
        })
        env.start()
        self.addCleanup(env.stop)
        accounts._store_for_path.cache_clear()
        self.addCleanup(accounts._store_for_path.cache_clear)
        self.store = accounts.get_multiuser_store()
        self.alice = make_user(self.store)
        self.bob = make_user(self.store, 'bob')
        self.a = self.alice['user']['id']
        self.b = self.bob['user']['id']
        self.keys = {self.a: self.store.create_key(self.a)['key'],
                     self.b: self.store.create_key(self.b)['key']}
        token = accounts.current_owner.set(None)
        self.addCleanup(accounts.current_owner.reset, token)
        self.database = Database()
        await self.database.initialize()
        self.addAsyncCleanup(self.database.close)
        for target in ('src.storage.database.get_database', 'src.credential_manager.get_database'):
            self.mock(target, AsyncMock(return_value=self.database))
        self.mode = self.mock('config.get_credential_select_mode', AsyncMock(return_value='random'))
        self.mock('src.multiuser.accounts.get_api_password', AsyncMock(return_value='legacy-admin-password'))
        self.mock('src.session_cache._redis_client', None)
        session_cache._memory_store.clear()
        self.addCleanup(session_cache._memory_store.clear)
        manager._reset_sticky()
        self.addCleanup(manager._reset_sticky)
        self.owner_by_email = {}
        await self.add('admin@example.test', accounts.ADMIN_OWNER)
        await self.add('alice@example.test', self.a)
        await self.add('bob@example.test', self.b)
        await self.add('mu_orphan@example.test', None)
        self.raw_key = session_cache.derive_session_key(MODEL, PROMPT)

    def mock(self, target, value):
        p = patch(target, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    async def add(self, email, owner):
        if owner not in (accounts.ADMIN_OWNER, None):
            self.store.reserve_credential(owner, email, 'antigravity')
        from src.multiuser.write_guard import member_upload_write
        # Seed a reserved row through the same explicit capability as ingestion;
        # do not bypass the real storage guard or silently ignore a failed write.
        grant = member_upload_write(owner, email) if owner not in (accounts.ADMIN_OWNER, None) else nullcontext()
        with grant:
            self.assertTrue(await self.database.store_credential(email, {
                'access_token': 'token:' + email,
                'refresh_token': 'refresh:' + email,
                'project_id': 'synthetic-project',
                'client_id': 'synthetic-client',
                'client_secret': 'synthetic-secret',
                'expiry': '2099-01-01T00:00:00+00:00',
            }))
        if owner not in (accounts.ADMIN_OWNER, None):
            self.store.credential_state(owner, email, 'antigravity', 'active')
        self.owner_by_email[email] = owner

    async def choose(self, owner, key=None):
        token = accounts.current_owner.set(owner)
        try:
            return await manager.get_valid_credential(MODEL, session_key=key)
        finally:
            accounts.current_owner.reset(token)

    async def test_real_email_database_concurrent_users_and_internal_admin(self):
        async def request(owner, email):
            for _ in range(12):
                await asyncio.sleep(0)
                selected = await self.choose(owner)
                self.assertEqual(selected[0], email)
                self.assertEqual(selected[1]['access_token'], 'token:' + email)
        await asyncio.gather(request(self.a, 'alice@example.test'),
                             request(self.b, 'bob@example.test'),
                             request(None, 'admin@example.test'))
        self.assertIsNone(accounts.current_owner.get())

    async def test_no_credentials_pending_deleted_disabled_and_cooldown_never_fallback(self):
        empty = make_user(self.store, 'empty')['user']['id']
        self.assertIsNone(await self.choose(empty))
        for state in ('pending', 'deleted'):
            self.store.credential_state(self.a, 'alice@example.test', 'antigravity', state)
            self.assertIsNone(await self.choose(self.a))
        self.store.credential_state(self.a, 'alice@example.test', 'antigravity', 'active')
        await self.database.update_credential_state('alice@example.test', {'disabled': True})
        self.assertIsNone(await self.choose(self.a))
        await self.database.update_credential_state('alice@example.test', {'disabled': False})
        await self.database.set_model_cooldown('alice@example.test', MODEL, time.time() + 3600)
        self.assertIsNone(await self.choose(self.a))
        # The family-level cooldown cannot be bypassed by a different Claude ID.
        token = accounts.current_owner.set(self.a)
        try:
            self.assertIsNone(await manager.get_valid_credential('claude-other'))
        finally:
            accounts.current_owner.reset(token)
        self.assertEqual((await self.choose(None))[0], 'admin@example.test')

    async def test_empty_member_pool_keeps_upstream500_for_stream_and_non_stream_without_network(self):
        empty = make_user(self.store, 'empty')['user']['id']
        token = accounts.current_owner.set(empty)
        try:
            with patch.object(antigravity, 'record_usage_log', AsyncMock()), \
                    patch.object(antigravity, '_record_request_body_detail', AsyncMock(return_value='')), \
                    patch.object(antigravity, 'post_async', AsyncMock()) as post, \
                    patch.object(antigravity, 'stream_post_async', AsyncMock()) as stream_post:
                body = {'model': MODEL, 'request': PROMPT}
                response = await antigravity.non_stream_request(body)
                self.assertEqual(response.status_code, 500)
                chunks = [chunk async for chunk in antigravity.stream_request(body)]
                self.assertEqual(len(chunks), 1)
                self.assertIsInstance(chunks[0], Response)
                self.assertEqual(chunks[0].status_code, 500)
                post.assert_not_called()
                stream_post.assert_not_called()
            self.assertEqual((await self.choose(None))[0], 'admin@example.test')
        finally:
            accounts.current_owner.reset(token)

    async def test_same_prompt_cache_is_namespaced_in_memory_and_redis(self):
        self.mode.return_value = 'cache'
        for redis in (None, FakeRedis()):
            with self.subTest(backend='redis' if redis else 'memory'):
                session_cache._memory_store.clear()
                with patch.object(session_cache, '_redis_client', redis):
                    for _ in range(3):
                        results = await asyncio.gather(self.choose(self.a, self.raw_key),
                                                       self.choose(self.b, self.raw_key))
                        self.assertEqual([item[0] for item in results],
                                         ['alice@example.test', 'bob@example.test'])
                    self.assertEqual(session_cache.memory_store_size(), 2)
                    self.assertNotIn(self.raw_key, session_cache._memory_store)
                    self.assertEqual({entry[0] for entry in session_cache._memory_store.values()},
                                     {'alice@example.test', 'bob@example.test'})
                    if redis:
                        self.assertEqual(len(redis.data), 2)

    async def test_poisoned_cache_cannot_read_or_return_another_users_credential(self):
        self.mode.return_value = 'cache'
        scoped = isolation.scoped_session_key(self.a, self.raw_key)
        await session_cache.set_session_binding(scoped, 'admin@example.test')
        with patch.object(self.database, 'get_available_credential_by_email',
                          wraps=self.database.get_available_credential_by_email) as reads:
            selected = await self.choose(self.a, self.raw_key)
        self.assertEqual(selected[0], 'alice@example.test')
        self.assertEqual([call.args[0] for call in reads.call_args_list], ['alice@example.test'])
        self.assertEqual(await session_cache.get_session_binding(scoped), 'alice@example.test')

    async def test_sticky_preserves_each_owners_own_pointer(self):
        self.mode.return_value = 'sticky'
        await self.add('alice-second@example.test', self.a)
        await self.add('bob-second@example.test', self.b)
        with patch('src.multiuser.isolation.random.shuffle', side_effect=lambda names: None):
            a_first = (await self.choose(self.a))[0]
            b_first = (await self.choose(self.b))[0]
        with patch('src.multiuser.isolation.random.shuffle', side_effect=lambda names: names.reverse()):
            for _ in range(5):
                self.assertEqual((await self.choose(self.a))[0], a_first)
                self.assertEqual((await self.choose(self.b))[0], b_first)
        await self.database.update_credential_state(a_first, {'disabled': True})
        a_next = (await self.choose(self.a))[0]
        self.assertNotEqual(a_next, a_first)
        self.assertEqual(self.owner_by_email[a_next], self.a)
        self.assertEqual((await self.choose(self.b))[0], b_first)

    async def test_ownership_store_failure_is_not_an_admin_fallback(self):
        with patch.object(self.store, 'owned_filenames', side_effect=RuntimeError('db unavailable')):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as error:
                await self.choose(self.a)
        self.assertEqual(error.exception.status_code, 503)

    async def test_deleted_during_selection_is_never_returned(self):
        original = self.database.get_available_credential_by_email

        async def deleting(email, **kwargs):
            result = await original(email, **kwargs)
            self.store.credential_state(self.a, email, 'antigravity', 'deleted')
            return result

        with patch.object(self.database, 'get_available_credential_by_email', side_effect=deleting):
            self.assertIsNone(await self.choose(self.a))

    async def test_foreign_refresh_never_reaches_google_or_writes(self):
        token = accounts.current_owner.set(self.a)
        try:
            with patch.object(manager.Credentials, 'refresh', AsyncMock()) as refresh, \
                    patch.object(self.database, 'store_credential', AsyncMock()) as write:
                self.assertIsNone(await manager.refresh_token({}, 'admin@example.test'))
                self.assertIsNone(await manager.refresh_token({}, 'bob@example.test'))
                refresh.assert_not_awaited()
                write.assert_not_awaited()
        finally:
            accounts.current_owner.reset(token)

    async def test_child_prewarm_retains_creator_owner_when_parent_changes(self):
        token = accounts.current_owner.set(self.a)
        try:
            task = asyncio.create_task(manager.get_valid_credential(MODEL))
            accounts.current_owner.set(self.b)
            self.assertEqual((await task)[0], 'alice@example.test')
            self.assertEqual((await manager.get_valid_credential(MODEL))[0], 'bob@example.test')
        finally:
            accounts.current_owner.reset(token)

    async def retry_scenario(self, stream):
        await self.add('alice-second@example.test', self.a)
        await self.add('bob-second@example.test', self.b)
        attempts = defaultdict(list)
        selection_tasks = defaultdict(list)
        parent_tasks = {}
        real_selector = manager.get_valid_credential

        async def observe_selection(*args, **kwargs):
            owner = accounts.current_owner.get()
            selection_tasks[owner].append(asyncio.current_task())
            return await real_selector(*args, **kwargs)

        async def record_error(email, *args, **kwargs):
            self.assertEqual(self.owner_by_email[email], accounts.current_owner.get())
            await self.database.update_credential_state(email, {'disabled': True})

        async def post(**kwargs):
            owner = accounts.current_owner.get()
            email = kwargs['headers']['Authorization'].removeprefix('Bearer token:')
            self.assertEqual(self.owner_by_email[email], owner)
            attempts[owner].append(email)
            await asyncio.sleep(0)
            if len(attempts[owner]) == 1:
                return httpx.Response(429, json={'error': 'synthetic quota exhausted'})
            return httpx.Response(200, json={'response': {'candidates': []}})

        async def stream_post(**kwargs):
            response = await post(**kwargs)
            if response.status_code == 429:
                yield Response(response.content, status_code=429)
            else:
                yield 'data: {"response":{"candidates":[]}}\n\n'

        patches = [
            patch.object(antigravity, 'get_valid_credential', side_effect=observe_selection),
            patch.object(antigravity, 'post_async', side_effect=post),
            patch.object(antigravity, 'stream_post_async', stream_post),
            patch.object(antigravity, 'get_antigravity_api_url', AsyncMock(return_value='https://never-called.invalid')),
            patch.object(antigravity, 'get_retry_config', AsyncMock(return_value={
                'max_retries': 1, 'retry_interval': 0, 'retry_enabled': True})),
            patch.object(antigravity, 'get_auto_ban_error_codes', AsyncMock(return_value=[])),
            patch.object(antigravity, 'handle_error_with_retry', AsyncMock(return_value=True)),
            patch.object(antigravity, 'parse_and_log_cooldown', AsyncMock(return_value=None)),
            patch.object(antigravity, 'record_api_call_error', side_effect=record_error),
            patch.object(antigravity, 'record_api_call_success', AsyncMock()),
            patch.object(antigravity, 'record_usage_log', AsyncMock()),
            patch.object(antigravity, '_record_request_body_detail', AsyncMock(return_value='')),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        async def request(owner):
            parent_tasks[owner] = asyncio.current_task()
            token = accounts.current_owner.set(owner)
            try:
                body = {'model': MODEL, 'request': PROMPT}
                if stream:
                    chunks = [chunk async for chunk in antigravity.stream_request(body)]
                    self.assertTrue(chunks)
                    self.assertTrue(all(isinstance(chunk, str) for chunk in chunks))
                else:
                    response = await antigravity.non_stream_request(body)
                    self.assertEqual(response.status_code, 200, response.body)
            finally:
                accounts.current_owner.reset(token)

        await asyncio.gather(request(self.a), request(self.b))
        self.assertEqual(set(attempts), {self.a, self.b})
        for owner in (self.a, self.b):
            self.assertEqual(len(attempts[owner]), 2)
            self.assertTrue(all(self.owner_by_email[email] == owner for email in attempts[owner]))
            self.assertGreaterEqual(len(selection_tasks[owner]), 2)
            self.assertTrue(any(task is not parent_tasks[owner] for task in selection_tasks[owner]),
                            'Regression must exercise the real child prewarm task')

    async def test_non_stream_internal_retry_and_prewarm_keep_owner(self):
        await self.retry_scenario(stream=False)

    async def test_stream_internal_retry_and_prewarm_keep_owner(self):
        await self.retry_scenario(stream=True)

    def app(self):
        from src.multiuser import install
        app = FastAPI()
        install(app)

        @app.get('/probe')
        async def probe(_=Depends(authenticate_flexible)):
            selected = await manager.get_valid_credential(MODEL, session_key=self.raw_key)
            return {'owner': accounts.current_owner.get(), 'email': selected[0] if selected else None}

        @app.get('/stream-probe')
        async def stream_probe(_=Depends(authenticate_flexible)):
            async def chunks():
                for _ in range(2):
                    selected = await asyncio.create_task(manager.get_valid_credential(MODEL))
                    yield json.dumps({'owner': accounts.current_owner.get(), 'email': selected[0]}) + '\n'
                    await asyncio.sleep(0)
            return StreamingResponse(chunks())

        @app.get('/unauthenticated-internal-probe')
        async def unauthenticated():
            # Even a new route forgetting auth must not inherit internal-admin None.
            return {'selected': await manager.get_valid_credential(MODEL)}
        return app

    async def test_http_concurrency_stream_lifetime_and_auth_header_variants(self):
        app = self.app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://inprocess') as client:
            async def request(owner, expected):
                key = self.keys[owner]
                for url in ('/probe', '/stream-probe'):
                    response = await client.get(url, headers={'Authorization': 'Bearer ' + key})
                    self.assertEqual(response.status_code, 200, response.text)
                    rows = [json.loads(line) for line in response.text.splitlines()]
                    for row in rows:
                        self.assertEqual(row, {'owner': owner, 'email': expected})
            await asyncio.gather(request(self.a, 'alice@example.test'), request(self.b, 'bob@example.test'))
            for kwargs in ({'headers': {'x-api-key': self.keys[self.a]}},
                           {'headers': {'x-goog-api-key': self.keys[self.a]}},
                           {'params': {'key': self.keys[self.a]}}):
                response = await client.get('/probe', **kwargs)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['email'], 'alice@example.test')
            response = await client.get('/unauthenticated-internal-probe')
            self.assertIsNone(response.json()['selected'])
        self.assertIsNone(accounts.current_owner.get())

    async def test_upload_identity_comes_from_refreshed_google_identity_not_json(self):
        async def refresh(credentials):
            credentials.access_token = 'verified-new-access-token'

        data = {'refresh_token': 'refresh:alice@example.test', 'project_id': 'synthetic-project',
                'access_token': 'attacker-supplied-access', 'email': ' ALICE@EXAMPLE.TEST '}
        with patch.object(storage.Credentials, 'refresh', refresh), \
                patch.object(storage.google_oauth_api, 'get_user_email',
                             AsyncMock(return_value=' ALICE@EXAMPLE.TEST ')) as identity:
            row = await accounts.store_member_credential(self.a, data, 'antigravity')
        self.assertEqual(row['filename'], 'alice@example.test')
        self.assertEqual(self.store.credential_owner(row['filename'], 'antigravity'), self.a)
        saved = await self.database.get_credential(row['filename'])
        self.assertEqual(saved['access_token'], 'verified-new-access-token')
        self.assertEqual(identity.await_args.args[0].access_token, 'verified-new-access-token')

    async def test_identity_mismatch_other_owner_admin_and_duplicate_token_cannot_be_claimed(self):
        from fastapi import HTTPException

        async def refresh(credentials):
            credentials.access_token = 'verified-new-access-token'

        scenarios = [
            # Client's JSON must not be allowed to override verified userinfo.
            (self.a, 'alice@example.test', {'email': 'admin@example.test', 'refresh_token': 'fresh-new'}, 400),
            (self.b, 'alice@example.test', {'refresh_token': 'fresh-new'}, 409),
            (self.a, 'admin@example.test', {'refresh_token': 'fresh-new'}, 409),
            # A legacy/synthetic-email row with the same refresh token must not
            # be silently duplicated under a fresh real-email key.
            (self.a, 'new@example.test', {'refresh_token': 'refresh:admin@example.test'}, 409),
        ]
        with patch.object(storage.Credentials, 'refresh', refresh):
            for owner, email, data, status in scenarios:
                with self.subTest(email=email, owner=owner, status=status):
                    with patch.object(storage.google_oauth_api, 'get_user_email', AsyncMock(return_value=email)), \
                            patch.object(self.database, 'store_credential', wraps=self.database.store_credential) as write:
                        with self.assertRaises(HTTPException) as error:
                            await accounts.store_member_credential(owner, {
                                'project_id': 'synthetic-project', **data}, 'antigravity')
                    self.assertEqual(error.exception.status_code, status)
                    write.assert_not_awaited()
        self.assertFalse(self.store.has_credential_mapping('new@example.test'))
        self.assertEqual((await self.database.get_credential('admin@example.test'))['access_token'],
                         'token:admin@example.test')

    async def test_storage_pending_capability_is_exact_task_scoped_and_deleted_is_never_writable(self):
        from src.multiuser.write_guard import member_upload_write
        email = 'alice@example.test'
        self.store.credential_state(self.a, email, 'antigravity', 'pending')
        stale = await self.database.get_credential(email)
        updated = {**stale, 'access_token': 'legitimate-upload-token'}
        self.assertFalse(await self.database.store_credential(email, updated))
        with member_upload_write(self.b, email):
            self.assertFalse(await self.database.store_credential(email, updated))
        with member_upload_write(self.a, email):
            # asyncio.timeout preserves the exact current Task, unlike wait_for.
            async with asyncio.timeout(5):
                self.assertTrue(await self.database.store_credential(email, updated))
            child = asyncio.create_task(self.database.store_credential(
                email, {**updated, 'access_token': 'inherited-grant-must-not-work'}))
            self.assertFalse(await asyncio.wait_for(child, timeout=5))
        self.assertEqual((await self.database.get_credential(email))['access_token'],
                         'legitimate-upload-token')
        self.store.credential_state(self.a, email, 'antigravity', 'deleted')
        await self.database.delete_credential(email)
        with member_upload_write(self.a, email):
            self.assertFalse(await self.database.store_credential(email, updated))
        self.assertIsNone(await self.database.get_credential(email))

    async def test_actual_refresh_and_member_deletion_are_serialized(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def refresh(credentials):
            entered.set()
            await finish.wait()
            credentials.access_token = 'synthetic-refreshed-token'

        token = accounts.current_owner.set(self.a)
        try:
            data = await self.database.get_credential('alice@example.test')
            with patch.object(manager.Credentials, 'refresh', refresh):
                refresh_task = asyncio.create_task(manager.refresh_token(data, 'alice@example.test'))
                await asyncio.wait_for(entered.wait(), timeout=5)
                delete_task = asyncio.create_task(accounts.delete_credential(
                    'alice@example.test', user=self.alice['user']))
                await asyncio.sleep(0)
                finish.set()
                await asyncio.gather(refresh_task, delete_task)
            self.assertIsNone(await self.database.get_credential('alice@example.test'))
            self.assertIsNone(await manager.get_valid_credential(MODEL))
            self.assertEqual(self.store.owned_filenames(self.a, 'antigravity'), [])
            self.assertEqual(self.store.credential_owner('alice@example.test', 'antigravity'), self.a)
        finally:
            finish.set()
            accounts.current_owner.reset(token)

    async def admin_refresh_delete_scenario(self, method, route):
        from src.panel import creds as panel
        entered, finish = asyncio.Event(), asyncio.Event()
        self.mock('src.panel.creds.get_database', AsyncMock(return_value=self.database))
        self.mock('src.utils.get_panel_password', AsyncMock(return_value='panel-password'))
        self.mock('src.panel.creds.get_antigravity_api_url', AsyncMock(return_value='https://never-called.invalid'))
        self.mock('src.panel.creds.fetch_project_id_and_tier',
                  AsyncMock(return_value=('synthetic-project', 'pro')))
        self.mock('src.panel.creds.fetch_quota_info', AsyncMock(return_value={'success': True, 'models': {}}))
        self.mock('src.panel.creds.post_async', AsyncMock(return_value=httpx.Response(200, json={'response': {}})))
        observed_owners = []

        async def refreshed(credentials):
            # This is the real panel handler, paused AFTER it read the old row
            # but BEFORE its later direct database.store_credential write.
            observed_owners.append(accounts.current_owner.get())
            entered.set()
            await finish.wait()
            credentials.access_token = 'admin-refreshed-stale-member-token'
            return True

        app = self.app()
        app.include_router(panel.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://inprocess') as client:
            with patch.object(panel.Credentials, 'refresh_if_needed', refreshed):
                admin_task = asyncio.create_task(client.request(method,
                    route + '/alice@example.test', headers={'Authorization': 'Bearer panel-password'}))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    response = await asyncio.wait_for(client.delete('/users/credentials/alice@example.test',
                        headers={'Authorization': 'Bearer ' + self.alice['token']}), timeout=5)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIsNone(await self.database.get_credential('alice@example.test'))
                    finish.set()
                    await asyncio.wait_for(admin_task, timeout=5)
                finally:
                    finish.set()
                    if not admin_task.done():
                        admin_task.cancel()
                        await asyncio.gather(admin_task, return_exceptions=True)
        self.assertEqual(observed_owners, [accounts.ADMIN_OWNER])
        self.assertIsNone(await self.database.get_credential('alice@example.test'),
                          'A stale admin panel refresh must never resurrect a member-deleted row')
        self.assertIsNone(await self.choose(self.a))
        self.assertEqual(self.store.owned_filenames(self.a, 'antigravity'), [])
        self.assertEqual(self.store.credential_owner('alice@example.test', 'antigravity'), self.a)

    async def test_admin_panel_quota_refresh_cannot_resurrect_member_deleted_credential(self):
        await self.admin_refresh_delete_scenario('GET', '/creds/quota')

    async def test_admin_panel_verify_refresh_cannot_resurrect_member_deleted_credential(self):
        await self.admin_refresh_delete_scenario('POST', '/creds/verify-project')

    async def test_admin_panel_test_refresh_cannot_resurrect_member_deleted_credential(self):
        await self.admin_refresh_delete_scenario('POST', '/creds/test')

    async def test_existing_legacy_sqlite_account_key_and_session_work_without_reissue(self):
        # This is deliberately a fixed PRE-MIGRATION schema, not accounts._SCHEMA
        # or a new-store.register() fixture that could mask a format regression.
        path = str(Path(self.store.path).with_name('legacy-members.sqlite3'))
        session = 'mus_' + 'S' * 43
        key = 'muk_' + 'K' * 43
        salt = '1a' * 16
        password = 'legacy-password-123'
        hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=16384, r=8, p=1, dklen=32).hex()
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                  password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                  created_at INTEGER NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
                CREATE TABLE api_keys (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                  user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
                CREATE TABLE credential_owners (mode TEXT NOT NULL, filename TEXT NOT NULL,
                  user_id TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(mode,filename));
            ''')
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,0)',
                       ('legacy-owner', 'legacy-alice', salt, hashed, 1700000000))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,NULL)',
                       (digest(session), 'legacy-owner', 1700000000, 9999999999))
            db.execute('INSERT INTO api_keys VALUES(?,?,?,?,?,?,NULL)',
                       ('legacy-key-id', digest(key), 'legacy-owner', 'pre-migration-key',
                        1700000000, 9999999999))
            db.execute("INSERT INTO credential_owners VALUES('antigravity','mu_old.json',?,0,'active')",
                       ('legacy-owner',))
        with patch.dict(os.environ, {'MULTIUSER_DB_PATH': path}):
            reopened = accounts.get_multiuser_store()
            self.assertEqual(reopened.resolve_session(session)['id'], 'legacy-owner')
            self.assertEqual(reopened.resolve_api_key(key), 'legacy-owner')
            self.assertEqual(reopened.login('LEGACY-ALICE', password)['user']['id'], 'legacy-owner')
            self.assertEqual(reopened.owned_filenames('legacy-owner', 'antigravity'), ['mu_old.json'])
            token = accounts.current_owner.set(None)
            try:
                self.assertEqual(await accounts.authenticate_user_api(key), 'legacy-owner')
                app = self.app()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://inprocess') as client:
                    headers = {'Authorization': 'Bearer ' + session}
                    response = await client.get('/users/me', headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()['id'], 'legacy-owner')
                    response = await client.get('/users/keys', headers=headers)
                    self.assertEqual(response.json()['keys'][0]['id'], 'legacy-key-id')
                    self.assertNotIn(key, response.text)
                    reopened.revoke_key('legacy-owner', 'legacy-key-id')
                    self.assertIsNone(reopened.resolve_api_key(key))
                    reopened.logout(session)
                    self.assertEqual((await client.get('/users/me', headers=headers)).status_code, 401)
            finally:
                accounts.current_owner.reset(token)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT password_salt,password_hash FROM users').fetchone(), (salt, hashed))

    async def test_http_invalid_revoked_and_expired_keys_never_fallback_admin(self):
        app = self.app()
        revoked = self.store.create_key(self.a)
        self.store.revoke_key(self.a, revoked['key_info']['id'])
        expired = self.store.create_key(self.a)
        with self.store.transaction() as db:
            db.execute('UPDATE api_keys SET expires_at=0 WHERE id=?', (expired['key_info']['id'],))
        invalid = ('invalid', 'muk_' + 'x' * 43, self.alice['token'],
                   self.store.create_invite()['invite'], revoked['key'], expired['key'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://inprocess') as client:
            self.assertEqual((await client.get('/probe', headers={
                'Authorization': 'Bearer legacy-admin-password'})).json()['email'], 'admin@example.test')
            for token in invalid:
                response = await client.get('/probe', headers={'Authorization': 'Bearer ' + token})
                self.assertEqual(response.status_code, 401, response.text)
                self.assertNotIn('admin@example.test', response.text)
            self.assertEqual((await client.get('/probe')).status_code, 401)
        self.assertIsNone(accounts.current_owner.get())
