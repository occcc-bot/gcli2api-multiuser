"""Run: python -m unittest discover -s tests -p 'test_multiuser*.py'.

Requires the project's FastAPI/httpx/python-multipart dependencies. Uses temporary
SQLite databases and an in-memory adapter; never reads production credentials.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import multiuser as mu

PASSWORD = 'correct-horse-battery-123'
VALID_CREDENTIAL = {'refresh_token': 'refresh-token-123', 'project_id': 'sample-project-123'}


def make_user(store, username='alice'):
    invite = store.create_invite()['invite']
    return store.register(invite, username, PASSWORD)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = mu.MultiuserStore(str(Path(self.temp.name) / 'multiuser.sqlite3'))
        self.addCleanup(self.temp.cleanup)

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            return db.execute(query, args).fetchall()

    def test_registration_normalizes_username_and_stores_only_hashes(self):
        invite = self.store.create_invite()['invite']
        registered = self.store.register(invite, ' Alice ', PASSWORD)
        self.assertEqual(registered['user']['username'], 'alice')
        self.assertEqual(registered['token'], registered['session_token'])
        salt, hashed = self.sql('SELECT password_salt,password_hash FROM users')[0]
        self.assertEqual(len(salt), 32)
        self.assertEqual(len(hashed), 64)
        self.assertNotIn(PASSWORD, (salt, hashed))
        self.assertNotEqual(self.sql('SELECT token_hash FROM invites')[0][0], invite)
        self.assertNotEqual(self.sql('SELECT token_hash FROM sessions')[0][0], registered['token'])
        key = self.store.create_key(registered['user']['id'])
        self.assertIsInstance(key['key'], str)
        self.assertEqual(key['key'], key['api_key'])
        self.assertNotEqual(self.sql('SELECT token_hash FROM api_keys')[0][0], key['key'])
        self.assertEqual(Path(self.store.path).stat().st_mode & 0o777, 0o600)

    def test_same_password_has_different_salt_and_hash(self):
        make_user(self.store, 'alice')
        make_user(self.store, 'bob')
        rows = self.sql('SELECT password_salt,password_hash FROM users')
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertNotEqual(rows[0][1], rows[1][1])

    def test_one_use_invite(self):
        invite = self.store.create_invite()['invite']
        self.store.register(invite, 'alice', PASSWORD)
        with self.assertRaises(HTTPException) as caught:
            self.store.register(invite, 'bob', PASSWORD)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM users')[0][0], 1)

    def test_concurrent_invite_consumption_exactly_one_winner(self):
        invite = self.store.create_invite()['invite']

        def register(name):
            try:
                return self.store.register(invite, name, PASSWORD)
            except HTTPException:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, ['alice', 'bob']))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM users')[0][0], 1)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM sessions')[0][0], 1)

    def test_duplicate_username_rolls_back_without_consuming_invite(self):
        make_user(self.store)
        invite = self.store.create_invite()['invite']
        with self.assertRaises(HTTPException) as caught:
            self.store.register(invite, 'ALICE', PASSWORD)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.sql('SELECT used_at FROM invites WHERE token_hash=?', (mu._digest(invite),)), [(None,)])
        self.store.register(invite, 'bob', PASSWORD)

    def test_expired_invite_rejected(self):
        invite = self.store.create_invite()['invite']
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE invites SET expires_at=?', (int(time.time()) - 1,))
        with self.assertRaises(HTTPException):
            self.store.register(invite, 'alice', PASSWORD)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM users')[0][0], 0)

    def test_login_and_session_expiry_logout(self):
        registration = make_user(self.store)
        self.assertEqual(self.store.resolve_session(registration['token']), registration['user'])
        login = self.store.login('ALICE', PASSWORD)
        self.assertNotEqual(login['token'], registration['token'])
        self.store.logout(login['token'])
        with self.assertRaises(HTTPException):
            self.store.resolve_session(login['token'])
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE sessions SET expires_at=0')
        with self.assertRaises(HTTPException):
            self.store.resolve_session(registration['token'])
        for username, password in [('alice', 'wrong'), ('nobody', PASSWORD)]:
            with self.assertRaises(HTTPException) as caught:
                self.store.login(username, password)
            self.assertEqual(caught.exception.status_code, 401)

    def test_key_expiry_revocation_and_cross_user_revoke(self):
        alice = make_user(self.store)['user']['id']
        bob = make_user(self.store, 'bob')['user']['id']
        key = self.store.create_key(alice)
        self.assertEqual(self.store.resolve_api_key(key['key']), alice)
        self.assertNotIn('token_hash', self.store.list_keys(alice)[0])
        self.assertEqual(self.store.list_keys(bob), [])
        with self.assertRaises(HTTPException) as caught:
            self.store.revoke_key(bob, key['key_info']['id'])
        self.assertEqual(caught.exception.status_code, 404)
        self.store.revoke_key(alice, key['key_info']['id'])
        self.assertIsNone(self.store.resolve_api_key(key['key']))
        another = self.store.create_key(alice)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE api_keys SET expires_at=0')
        self.assertIsNone(self.store.resolve_api_key(another['key']))

    def test_disabled_user_loses_session_and_api_access(self):
        registration = make_user(self.store)
        key = self.store.create_key(registration['user']['id'])
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE users SET disabled=1')
        with self.assertRaises(HTTPException):
            self.store.resolve_session(registration['token'])
        with self.assertRaises(HTTPException):
            self.store.login('alice', PASSWORD)
        self.assertIsNone(self.store.resolve_api_key(key['key']))

    def test_ownership_modes_pending_deleted_and_orphan(self):
        alice = make_user(self.store)['user']['id']
        bob = make_user(self.store, 'bob')['user']['id']
        filename = 'mu_member.json'
        names = ['legacy.json', filename, 'mu_orphan.json']
        self.store.reserve_credential(alice, filename, 'antigravity')
        self.assertEqual(self.store.credential_owner(filename, 'antigravity'), alice)
        self.assertEqual(self.store.credential_owner('mu_orphan.json', 'antigravity'), mu.ORPHAN_OWNER)
        self.assertEqual(self.store.credential_owner('legacy.json', 'antigravity'), mu.ADMIN_OWNER)
        self.assertEqual(self.store.owned_filenames(mu.ADMIN_OWNER, 'antigravity', names), ['legacy.json'])
        self.assertEqual(self.store.owned_filenames(alice, 'antigravity', names), [])
        self.store.credential_state(alice, filename, 'antigravity', 'active')
        self.assertEqual(self.store.owned_filenames(alice, 'antigravity', names), [filename])
        self.assertEqual(self.store.owned_filenames(bob, 'antigravity', names), [])
        self.assertEqual(self.store.owned_filenames(alice, 'geminicli', names), [])
        self.assertEqual(self.store.owned_filenames(None, 'antigravity', names), [])
        self.store.credential_state(alice, filename, 'antigravity', 'deleted')
        self.assertEqual(self.store.owned_filenames(alice, 'antigravity', names), [])
        self.assertEqual(self.store.credential_owner(filename, 'antigravity'), alice)
        self.assertEqual(self.store.owned_filenames(mu.ADMIN_OWNER, 'antigravity', names), ['legacy.json'])

    def test_database_loss_does_not_make_member_files_admin(self):
        fresh = mu.MultiuserStore(str(Path(self.temp.name) / 'fresh.sqlite3'))
        self.assertEqual(fresh.credential_owner('mu_123.json', 'antigravity'), mu.ORPHAN_OWNER)
        self.assertEqual(fresh.owned_filenames(mu.ADMIN_OWNER, 'antigravity', ['mu_123.json', 'old.json']), ['old.json'])

    def test_validation_rejects_reserved_user_and_weak_password(self):
        invite = self.store.create_invite()['invite']
        for username, password in [('__admin__', PASSWORD), ('a/b/c', PASSWORD), ('alice', 'short')]:
            with self.assertRaises(HTTPException):
                self.store.register(invite, username, password)
        self.store.register(invite, 'alice', PASSWORD)


class APIAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'MULTIUSER_DB_PATH': str(Path(self.temp.name) / 'users.sqlite3'),
                                          'MULTIUSER_ALLOW_LEGACY_API_PASSWORD': 'true'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(mu._store_for_path.cache_clear)
        self.store = mu.get_multiuser_store()
        self.alice = make_user(self.store)
        self.key = self.store.create_key(self.alice['user']['id'])['key']
        self.legacy = patch.object(mu, 'get_api_password', AsyncMock(return_value='legacy-password'))
        self.legacy.start()
        self.addCleanup(self.legacy.stop)
        mu.current_owner.set(None)

    async def test_member_key_and_exact_legacy_password(self):
        owner = await mu.authenticate_user_api(self.key)
        self.assertEqual(owner, self.alice['user']['id'])
        self.assertEqual(mu.current_owner.get(), owner)
        self.assertEqual(await mu.authenticate_user_api('legacy-password'), mu.ADMIN_OWNER)
        self.assertEqual(mu.current_owner.get(), mu.ADMIN_OWNER)

    async def test_invalid_auth_clears_context_no_fallback(self):
        tokens = [self.alice['token'], self.store.create_invite()['invite'],
                  'muk_' + 'x' * 43, 'unknown', '', None]
        for token in tokens:
            mu.current_owner.set(mu.ADMIN_OWNER)
            with self.assertRaises(HTTPException) as caught:
                await mu.authenticate_user_api(token)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertIsNone(mu.current_owner.get())

    async def test_legacy_can_be_disabled(self):
        with self.assertRaises(HTTPException):
            await mu.authenticate_user_api('legacy-password', allow_legacy=False)
        with patch.dict(os.environ, {'MULTIUSER_ALLOW_LEGACY_API_PASSWORD': 'false'}):
            with self.assertRaises(HTTPException):
                await mu.authenticate_user_api('legacy-password')
        self.assertEqual(await mu.authenticate_user_api(self.key, allow_legacy=False), self.alice['user']['id'])

    async def test_member_prefixes_never_match_legacy_password(self):
        for token in [self.alice['token'], 'muk_' + 'x' * 43, self.store.create_invite()['invite']]:
            with patch.object(mu, 'get_api_password', AsyncMock(return_value=token)):
                with self.assertRaises(HTTPException):
                    await mu.authenticate_user_api(token)

    async def test_contexts_are_isolated_across_tasks(self):
        bob = make_user(self.store, 'bob')
        bob_key = self.store.create_key(bob['user']['id'])['key']

        async def auth(token):
            owner = await mu.authenticate_user_api(token)
            await asyncio.sleep(0)
            self.assertEqual(mu.current_owner.get(), owner)
            return owner

        self.assertEqual(await asyncio.gather(auth(self.key), auth(bob_key)),
                         [self.alice['user']['id'], bob['user']['id']])
        self.assertIsNone(mu.current_owner.get())


class FakeAdapter:
    def __init__(self):
        self.data = {('antigravity', 'legacy-admin.json'): dict(VALID_CREDENTIAL)}
        self.fail_store = False
        self.fail_delete = False
        self.expected_owner = None
        self.writes = []

    async def store_credential(self, filename, data, mode='geminicli'):
        # Tests enforce mapping before data is visible to the storage adapter.
        if self.expected_owner is not None:
            assert mu.credential_owner(filename, mode) == self.expected_owner
        self.writes.append((mode, filename))
        self.data[(mode, filename)] = data
        return not self.fail_store  # simulate partial write with failure

    async def list_credentials(self, mode='geminicli'):
        return [filename for stored_mode, filename in self.data if stored_mode == mode]

    async def delete_credential(self, filename, mode='geminicli'):
        if self.fail_delete:
            return False
        return self.data.pop((mode, filename), None) is not None

    async def get_credential(self, filename, mode='geminicli'):
        return self.data.get((mode, filename))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'MULTIUSER_DB_PATH': str(Path(self.temp.name) / 'users.sqlite3')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(mu._store_for_path.cache_clear)
        self.store = mu.get_multiuser_store()
        self.adapter = FakeAdapter()
        adapter_patch = patch.object(mu, 'get_storage_adapter', AsyncMock(return_value=self.adapter))
        adapter_patch.start()
        self.addCleanup(adapter_patch.stop)
        panel_patch = patch('src.utils.get_panel_password', AsyncMock(return_value='panel-password'))
        panel_patch.start()
        self.addCleanup(panel_patch.stop)
        app = FastAPI()
        app.include_router(mu.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.alice = make_user(self.store)
        self.bob = make_user(self.store, 'bob')
        self.alice_headers = {'Authorization': 'Bearer ' + self.alice['token']}
        self.bob_headers = {'Authorization': 'Bearer ' + self.bob['token']}

    def upload(self, headers=None, payload=None, mode='antigravity', filename='../../evil.json'):
        self.adapter.expected_owner = self.alice['user']['id']
        return self.client.post('/users/credentials', headers=headers or self.alice_headers,
                                data={'mode': mode},
                                files=[('files', (filename, json.dumps(payload or VALID_CREDENTIAL), 'application/json'))])

    def test_frontend_login_me_and_logout_schemas(self):
        response = self.client.post('/users/login', json={'username': 'alice', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        token = response.json()['token']
        headers = {'Authorization': 'Bearer ' + token}
        response = self.client.get('/users/me', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['username'], 'alice')
        self.assertEqual(self.client.post('/users/logout', headers=headers).json(), {'success': True})
        self.assertEqual(self.client.get('/users/me', headers=headers).status_code, 401)

    def test_only_panel_password_creates_invites(self):
        for headers in [{}, self.alice_headers, self.bob_headers,
                        {'Authorization': 'Bearer ' + self.store.create_key(self.alice['user']['id'])['key']}]:
            self.assertIn(self.client.post('/users/invites', json={'expires_hours': 72}, headers=headers).status_code, (401, 403))
        response = self.client.post('/users/invites', json={'expires_hours': 72},
                                    headers={'Authorization': 'Bearer panel-password'})
        self.assertEqual(response.status_code, 200)
        invite = response.json()
        self.assertTrue(invite['invite'].startswith(mu.INVITE_PREFIX))
        self.assertAlmostEqual(invite['expires_at'] - int(time.time()), 72 * 3600, delta=2)
        register = self.client.post('/users/register', json={'invite': invite['invite'], 'username': 'charlie', 'password': PASSWORD})
        self.assertEqual(register.status_code, 200)
        self.assertIsInstance(register.json()['token'], str)
        self.assertEqual(self.client.post('/users/register', json={'invite': invite['invite'], 'username': 'david', 'password': PASSWORD}).status_code, 400)

    def test_keys_creation_listing_and_cross_tenant_revocation(self):
        response = self.client.post('/users/keys', headers=self.alice_headers, json={'name': 'my-key'})
        self.assertEqual(response.status_code, 200)
        key = response.json()
        self.assertIsInstance(key['key'], str)
        rows = self.client.get('/users/keys', headers=self.alice_headers).json()['keys']
        self.assertEqual(rows[0]['name'], 'my-key')
        self.assertNotIn(key['key'], json.dumps(rows))
        self.assertEqual(self.client.get('/users/keys', headers=self.bob_headers).json()['keys'], [])
        key_id = rows[0]['id']
        self.assertEqual(self.client.delete('/users/keys/' + key_id, headers=self.bob_headers).status_code, 404)
        self.assertEqual(self.client.delete('/users/keys/' + key_id, headers=self.alice_headers).status_code, 200)
        self.assertIsNone(self.store.resolve_api_key(key['key']))

    def test_only_member_sessions_access_member_management(self):
        key = self.store.create_key(self.alice['user']['id'])['key']
        for token in [key, 'panel-password', 'legacy-password']:
            response = self.client.get('/users/me', headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get('/users/me').status_code, 401)
        self.assertEqual(self.client.get('/users/keys').status_code, 401)

    def test_upload_list_delete_isolation_and_generated_filenames(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        filename = response.json()['credential']['filename']
        self.assertRegex(filename, r'^mu_[0-9a-f]{32}\.json$')
        self.assertNotIn('evil', filename)
        rows = self.client.get('/users/credentials', headers=self.alice_headers).json()['credentials']
        self.assertEqual([row['filename'] for row in rows], [filename])
        self.assertNotIn('refresh_token', json.dumps(rows))
        self.assertEqual(self.client.get('/users/credentials', headers=self.bob_headers).json()['credentials'], [])
        self.assertEqual(self.client.delete('/users/credentials/' + filename, headers=self.bob_headers).status_code, 404)
        self.assertEqual(self.client.delete('/users/credentials/legacy-admin.json', headers=self.alice_headers).status_code, 404)
        self.assertEqual(self.client.delete('/users/credentials/' + filename, headers=self.alice_headers).status_code, 200)
        self.assertNotIn(('antigravity', filename), self.adapter.data)
        self.assertEqual(mu.credential_owner(filename, 'antigravity'), self.alice['user']['id'])
        self.assertEqual(mu.owned_filenames(mu.ADMIN_OWNER, 'antigravity', [filename, 'legacy-admin.json']), ['legacy-admin.json'])
        self.assertEqual(self.client.delete('/users/credentials/' + filename, headers=self.alice_headers).status_code, 200)

    def test_batch_files_and_single_file_alias_mode(self):
        self.adapter.expected_owner = self.alice['user']['id']
        response = self.client.post('/users/credentials', headers=self.alice_headers,
                                    data={'mode': 'antigravity'}, files=[
                                        ('files', ('first.json', json.dumps(VALID_CREDENTIAL))),
                                        ('files', ('second.json', json.dumps(VALID_CREDENTIAL)))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['credentials']), 2)
        response = self.client.post('/users/credentials/upload?mode=geminicli', headers=self.alice_headers,
                                    data={'mode': 'antigravity'}, files={'file': ('one.json', json.dumps(VALID_CREDENTIAL))})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['credential']['mode'], 'geminicli')

    def test_bad_uploads_leave_no_ownership_or_adapter_writes(self):
        invalids = [b'not json', b'[]', b'{"refresh_token":"abc"}',
                    json.dumps({'refresh_token': 'abc', 'project_id': '../evil'}).encode(),
                    json.dumps({**VALID_CREDENTIAL, 'token_uri': 'http://localhost/private'}).encode(),
                    b'x' * (mu.MAX_UPLOAD_BYTES + 1)]
        for payload in invalids:
            response = self.client.post('/users/credentials', headers=self.alice_headers,
                                        files={'file': ('one.json', payload)})
            self.assertIn(response.status_code, (400, 413))
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.store.list_credentials(self.alice['user']['id'], 'antigravity'), [])
        self.assertEqual(self.upload(mode='bad-mode').status_code, 400)

    def test_partial_adapter_failure_remains_owned_not_admin(self):
        self.adapter.fail_store = True
        response = self.upload()
        self.assertEqual(response.status_code, 503)
        mode, filename = self.adapter.writes[-1]
        self.assertIn((mode, filename), self.adapter.data)
        self.assertEqual(mu.credential_owner(filename, mode), self.alice['user']['id'])
        self.assertEqual(mu.owned_filenames(mu.ADMIN_OWNER, mode, [filename, 'legacy-admin.json']), ['legacy-admin.json'])
        self.assertEqual(mu.owned_filenames(self.alice['user']['id'], mode, [filename]), [])

    def test_failed_delete_is_hidden_and_can_be_retried(self):
        filename = self.upload().json()['credential']['filename']
        self.adapter.fail_delete = True
        self.assertEqual(self.client.delete('/users/credentials/' + filename, headers=self.alice_headers).status_code, 503)
        self.assertEqual(mu.owned_filenames(self.alice['user']['id'], 'antigravity', [filename]), [])
        self.assertEqual(mu.owned_filenames(mu.ADMIN_OWNER, 'antigravity', [filename]), [])
        self.adapter.fail_delete = False
        self.assertEqual(self.client.delete('/users/credentials/' + filename, headers=self.alice_headers).status_code, 200)

    def test_whitelisted_oauth_fields(self):
        response = self.upload(payload={**VALID_CREDENTIAL, 'token_uri': 'https://oauth2.googleapis.com/token',
                                        'scopes': ['https://www.googleapis.com/auth/cloud-platform'],
                                        'malicious_extra_url': 'http://internal/'})
        self.assertEqual(response.status_code, 200)
        filename = response.json()['credential']['filename']
        data = self.adapter.data[('antigravity', filename)]
        self.assertNotIn('malicious_extra_url', data)
        self.assertIn('scopes', data)


if __name__ == '__main__':
    unittest.main()
