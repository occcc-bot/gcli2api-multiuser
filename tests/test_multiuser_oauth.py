"""Synthetic member OAuth tests; no listeners, Google traffic or real credentials.

Run: python -m unittest discover -s tests -p 'test_multiuser*.py'
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from fastapi import HTTPException
from src import member_oauth as oauth
from src import multiuser as mu
import test_multiuser as fixtures


class FakeFlow:
    def __init__(self, **kwargs):
        self.options = kwargs

    def get_auth_url(self, state):
        return 'https://accounts.google.com/o/oauth2/auth?' + urlencode({
            'state': state, 'redirect_uri': self.options['redirect_uri']})

    async def exchange_code(self, code):
        return SimpleNamespace(access_token='synthetic-access', refresh_token='synthetic-refresh',
                               expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))


class MemberOAuthTests(unittest.TestCase):
    # Reuse only fixture setup, not inheritance (which repeats unrelated tests).
    def setUp(self):
        fixtures.RouterTests.setUp(self)
        self.client.app.include_router(oauth.router)
        self.adapter.expected_owner = self.alice['user']['id']
        for name in ('ANTIGRAVITY_CLIENT_ID', 'ANTIGRAVITY_CLIENT_SECRET'):
            p = patch.object(oauth, name, 'synthetic-test-client')
            p.start()
            self.addCleanup(p.stop)
        self.adapter.update_credential_state = AsyncMock(return_value=True)
        self.flow_patch = patch.object(oauth, 'Flow', FakeFlow)
        self.flow_patch.start()
        self.addCleanup(self.flow_patch.stop)
        self.fetch = AsyncMock(return_value=('sample-project-123', 'pro'))
        for name, replacement in [('fetch_project_id_and_tier', self.fetch),
                                  ('get_antigravity_api_url', AsyncMock(return_value='https://synthetic.invalid'))]:
            p = patch.object(oauth, name, replacement)
            p.start()
            self.addCleanup(p.stop)

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            return db.execute(query, args).fetchall()

    def start(self, headers=None):
        response = self.client.post('/users/oauth/start', headers=headers or self.alice_headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def callback(self, flow, **overrides):
        params = {'code': 'synthetic-code', 'state': flow['state']}
        params.update(overrides)
        return flow['redirect_uri'] + '/?' + urlencode(params)

    def complete(self, flow, headers=None, callback=None):
        return self.client.post('/users/oauth/complete', headers=headers or self.alice_headers,
                                json={'callback_url': callback or self.callback(flow)})

    def test_authentication_is_member_sessions_only(self):
        api_key = self.store.create_key(self.alice['user']['id'])['key']
        for headers in ({}, {'Authorization': 'Bearer panel-password'},
                        {'Authorization': 'Bearer ' + api_key}):
            for path, body in [('/users/oauth/start', {}),
                               ('/users/oauth/complete', {'callback_url': 'http://localhost:11451/'})]:
                self.assertEqual(self.client.post(path, headers=headers, json=body).status_code, 401)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM member_oauth_flows'), [(0,)])

    def test_start_accepts_empty_body_and_rejects_client_flow_overrides(self):
        response = self.client.post('/users/oauth/start', headers=self.alice_headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/users/oauth/start', headers=self.alice_headers,
                                    json={'redirect_uri': 'https://attacker.invalid'})
        self.assertEqual(response.status_code, 400)

    def test_start_metadata_and_restart_supersedes_only_own_flow(self):
        old = self.start()
        bob = self.start(self.bob_headers)
        new = self.start()
        self.assertEqual(set(new), {'auth_url', 'state', 'redirect_uri', 'expires_at'})
        self.assertEqual(new['redirect_uri'], 'http://localhost:11451')
        self.assertIsInstance(new['expires_at'], int)
        self.assertIn(new['state'], new['auth_url'])
        self.assertEqual(self.complete(old).status_code, 400)
        self.assertEqual(self.sql('SELECT state FROM member_oauth_flows WHERE user_id=?',
                                 (self.bob['user']['id'],)), [(bob['state'],)])
        self.assertEqual(self.complete(new).status_code, 200)

    def test_success_is_owned_reserved_before_write_and_state_initialized(self):
        response = self.complete(self.start())
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(set(data), {'success', 'credential'})
        credential = data['credential']
        filename = credential['filename']
        self.assertTrue(filename.startswith('mu_'))
        self.assertEqual(self.store.credential_owner(filename, 'antigravity'), self.alice['user']['id'])
        self.assertEqual(self.store.owned_filenames(self.alice['user']['id'], 'antigravity'), [filename])
        self.assertEqual(self.store.owned_filenames(self.bob['user']['id'], 'antigravity'), [])
        self.assertEqual(self.store.owned_filenames(mu.ADMIN_OWNER, 'antigravity', [filename]), [])
        saved = self.adapter.data[('antigravity', filename)]
        self.assertEqual(saved['refresh_token'], 'synthetic-refresh')
        self.assertEqual(saved['project_id'], 'sample-project-123')
        self.assertEqual(saved['scopes'], oauth.ANTIGRAVITY_SCOPES)
        args, kwargs = self.adapter.update_credential_state.call_args
        self.assertEqual(args[0], filename)
        self.assertEqual(args[1]['tier'], 'pro')
        self.assertEqual(kwargs, {'mode': 'antigravity'})
        self.assertNotIn('synthetic-refresh', response.text)
        self.assertNotIn('synthetic-access', response.text)
        # Only flow metadata, not codes/tokens, is persisted in member SQLite.
        rows = self.sql('SELECT * FROM member_oauth_flows')
        self.assertNotIn('synthetic-code', str(rows))
        self.assertNotIn('synthetic-access', str(rows))
        self.assertNotIn('synthetic-refresh', str(rows))

    def test_wrong_owner_cannot_consume_legitimate_flow(self):
        flow = self.start()
        self.assertEqual(self.complete(flow, self.bob_headers).status_code, 400)
        self.assertEqual(self.sql('SELECT status FROM member_oauth_flows'), [('pending',)])
        self.assertEqual(self.complete(flow).status_code, 200)
        self.assertEqual(self.complete(flow).status_code, 400)
        self.assertEqual(self.fetch.await_count, 1)

    def test_expired_state_and_replay_rejected(self):
        flow = self.start()
        self.sql('UPDATE member_oauth_flows SET expires_at=0')
        self.assertEqual(self.complete(flow).status_code, 400)
        self.fetch.assert_not_awaited()
        fresh = self.start()
        self.assertEqual(self.sql('SELECT state FROM member_oauth_flows'), [(fresh['state'],)])
        self.assertEqual(self.complete(fresh).status_code, 200)
        self.assertEqual(self.complete(fresh).status_code, 400)

    def test_atomic_concurrent_claim_has_exactly_one_winner(self):
        flow = self.start()
        def claim(_):
            try:
                self.store.claim_oauth_flow(self.alice['user']['id'], flow['state'], flow['redirect_uri'])
                return True
            except HTTPException:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim, range(8))), 1)

    def test_callback_validation_does_not_consume_flow(self):
        flow = self.start()
        callback = self.callback(flow)
        malformed = [callback.replace('localhost', '127.0.0.1'),
                     callback.replace(':11451', ':11452'), callback.replace('http:', 'https:'),
                     callback.replace('/?', '/other?'), callback + '#fragment',
                     callback.replace('localhost', 'attacker@localhost'),
                     callback + '&code=second', callback + '&state=second',
                     callback + '&error=access_denied', self.callback(flow, code=''),
                     callback.replace('synthetic-code', '%00secret'),
                     callback.replace('synthetic-code', '%GG'), 'not-a-url', None, {},
                     callback.replace(flow['state'], 'bad-state')]
        for value in malformed:
            response = self.client.post('/users/oauth/complete', headers=self.alice_headers,
                                        json={'callback_url': value})
            self.assertEqual(response.status_code, 400, str(value))
            self.assertNotIn('synthetic-code', response.text)
            self.assertEqual(self.sql('SELECT status FROM member_oauth_flows'), [('pending',)])
        self.assertEqual(self.complete(flow).status_code, 200)

    def test_project_fetch_failure_needs_reauthorization_no_fallback(self):
        for result in [(None, None), RuntimeError('sensitive-token-detail')]:
            flow = self.start()
            self.fetch.side_effect = result if isinstance(result, Exception) else None
            self.fetch.return_value = result
            response = self.complete(flow)
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('sensitive-token-detail', response.text)
            self.assertEqual(self.adapter.writes, [])
            self.assertEqual(self.complete(flow).status_code, 400)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM credential_owners'), [(0,)])

    def test_token_exchange_failure_generic_and_one_use(self):
        flow = self.start()
        with patch.object(FakeFlow, 'exchange_code', AsyncMock(side_effect=RuntimeError('secret-code-token'))):
            response = self.complete(flow)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('secret-code-token', response.text)
        self.assertEqual(self.complete(flow).status_code, 400)
        self.fetch.assert_not_awaited()

    def test_storage_failure_retains_pending_reservation(self):
        self.adapter.fail_store = True
        flow = self.start()
        self.assertEqual(self.complete(flow).status_code, 503)
        [(filename, owner, status)] = self.sql('SELECT filename,user_id,state FROM credential_owners')
        self.assertEqual((owner, status), (self.alice['user']['id'], 'pending'))
        self.assertIn(('antigravity', filename), self.adapter.data)
        self.assertEqual(self.store.owned_filenames(owner, 'antigravity'), [])
        self.assertEqual(self.store.owned_filenames(mu.ADMIN_OWNER, 'antigravity', [filename]), [])
        self.assertEqual(self.complete(flow).status_code, 400)

    def test_reservation_failure_never_reaches_adapter(self):
        with patch.object(self.store, 'reserve_credential', side_effect=RuntimeError('private-detail')):
            response = self.complete(self.start())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-detail', response.text)
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM credential_owners'), [(0,)])

    def test_adapter_state_failure_does_not_activate_credential(self):
        self.adapter.update_credential_state.side_effect = RuntimeError('sensitive-token-detail')
        response = self.complete(self.start())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('sensitive-token-detail', response.text)
        self.assertEqual(self.sql('SELECT state FROM credential_owners'), [('pending',)])

    def test_global_metadata_limit(self):
        for _ in range(200):
            metadata = self.store.create_oauth_flow(self.alice['user']['id'], oauth.REDIRECT_URI)
            self.store.claim_oauth_flow(self.alice['user']['id'], metadata['state'], oauth.REDIRECT_URI)
        response = self.client.post('/users/oauth/start', headers=self.alice_headers, json={})
        self.assertEqual(response.status_code, 429)

    def test_private_helper_logging_is_context_local(self):
        from unittest.mock import Mock
        original = Mock()
        proxy = oauth._PrivateOAuthLogger(original)
        proxy.error('normal')
        token = oauth._private_oauth.set(True)
        try:
            proxy.error('secret-token')
        finally:
            oauth._private_oauth.reset(token)
        proxy.error('normal-again')
        self.assertEqual(original.error.call_count, 2)


if __name__ == '__main__':
    unittest.main()
