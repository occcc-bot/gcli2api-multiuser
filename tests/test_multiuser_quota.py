"""Synthetic quota isolation tests; no Google traffic or production credentials."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src import member_quota as quota
import test_multiuser as fixtures


class MemberQuotaTests(unittest.TestCase):
    def setUp(self):
        # Composition, not inheritance: do not repeat the router test suite.
        fixtures.RouterTests.setUp(self)
        self.client.app.include_router(quota.router)
        self.filename = 'mu_synthetic.json'
        self.owner = self.alice['user']['id']
        self.store.reserve_credential(self.owner, self.filename, 'antigravity')
        self.store.credential_state(self.owner, self.filename, 'antigravity', 'active')
        self.data = {'token': 'synthetic-owner-token', 'refresh_token': 'synthetic-refresh',
                     'expiry': '2099-01-01T00:00:00+00:00', 'project_id': 'sample-project-123',
                     'client_id': 'synthetic-client', 'client_secret': 'synthetic-secret',
                     'scopes': ['scope-a'], 'token_uri': 'https://synthetic.invalid/token',
                     'metadata': {'note': 'keep me'}, 'disabled': True}
        self.adapter.data[('antigravity', self.filename)] = deepcopy(self.data)
        self.adapter.get_credential = AsyncMock(wraps=self.adapter.get_credential)
        self.adapter.store_credential = AsyncMock(wraps=self.adapter.store_credential)
        self.adapter.update_credential_state = AsyncMock()
        self.get_adapter = AsyncMock(return_value=self.adapter)
        self.models = {'synthetic-model': {'remaining': 0.75, 'resetTime': '01-01 08:00',
                                          'resetTimeRaw': '2099-01-01T00:00:00Z'}}
        self.fetch = AsyncMock(return_value={'success': True, 'models': deepcopy(self.models)})
        for name, value in [('get_storage_adapter', self.get_adapter),
                            ('fetch_quota_info', self.fetch)]:
            p = patch.object(quota, name, value)
            p.start()
            self.addCleanup(p.stop)

    def get(self, headers=None, filename=None, mode='antigravity'):
        return self.client.get('/users/credentials/' + (filename or self.filename) + '/quota',
                               params={'mode': mode},
                               headers=self.alice_headers if headers is None else headers)

    def assert_error(self, response, status):
        self.assertEqual(response.status_code, status, response.text)
        for secret in ('synthetic-owner-token', 'synthetic-refresh', 'synthetic-secret',
                       'sensitive-upstream-detail'):
            self.assertNotIn(secret, response.text)

    def refreshed(self, credentials):
        credentials.access_token = 'synthetic-new-token'
        credentials.refresh_token = 'synthetic-new-refresh'
        credentials.expires_at = datetime(2099, 2, 1, tzinfo=timezone.utc)
        return True

    def refresh_patch(self, callback=None):
        async def refresh(credentials):
            return (callback or self.refreshed)(credentials)
        return patch.object(quota.Credentials, 'refresh_if_needed', refresh)

    def test_owner_success_exact_token_and_public_schema(self):
        response = self.get()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'success': True, 'filename': self.filename,
                                          'models': self.models})
        self.fetch.assert_awaited_once_with('synthetic-owner-token')
        self.adapter.get_credential.assert_awaited_once_with(self.filename, mode='antigravity')

    def test_second_owner_uses_only_their_token(self):
        name = 'mu_bob.json'
        bob = self.bob['user']['id']
        self.store.reserve_credential(bob, name, 'antigravity')
        self.store.credential_state(bob, name, 'antigravity', 'active')
        self.adapter.data[('antigravity', name)] = {**self.data, 'token': 'bob-only-token'}
        self.assertEqual(self.get(self.bob_headers, name).status_code, 200)
        self.fetch.assert_awaited_once_with('bob-only-token')

    def test_cross_user_never_reaches_adapter(self):
        self.assert_error(self.get(self.bob_headers), 404)
        self.get_adapter.assert_not_awaited()
        self.fetch.assert_not_awaited()

    def test_sessions_only_all_auth_classes(self):
        key = self.store.create_key(self.owner)['key']
        for headers in ({}, {'Authorization': 'Bearer panel-password'},
                        {'Authorization': 'Bearer ' + key},
                        {'Authorization': 'Basic ' + self.alice['token']},
                        {'Authorization': 'Bearer invalid-session'}):
            with self.subTest(headers=headers):
                self.assert_error(self.get(headers), 401)
        self.get_adapter.assert_not_awaited()
        self.fetch.assert_not_awaited()

    def test_revoked_session_rejected(self):
        self.store.logout(self.alice['token'])
        self.assert_error(self.get(), 401)
        self.get_adapter.assert_not_awaited()

    def test_other_modes_rejected_before_storage(self):
        for mode in ('geminicli', '', 'unknown'):
            self.assert_error(self.get(mode=mode), 400)
        self.get_adapter.assert_not_awaited()

    def test_pending_deleted_never_read_or_refresh(self):
        with patch.object(quota.Credentials, 'refresh_if_needed', AsyncMock()) as refresh:
            for state in ('pending', 'deleted'):
                self.store.credential_state(self.owner, self.filename, 'antigravity', state)
                self.assert_error(self.get(), 404)
            refresh.assert_not_awaited()
        self.get_adapter.assert_not_awaited()

    def test_admin_orphan_unknown_and_wrong_mode_mapping_are_hidden(self):
        self.store.reserve_credential(self.owner, 'mu_wrong-mode.json', 'geminicli')
        self.store.credential_state(self.owner, 'mu_wrong-mode.json', 'geminicli', 'active')
        for filename in ('legacy-admin.json', 'mu_orphan.json', 'unknown.json', 'mu_wrong-mode.json'):
            self.assert_error(self.get(filename=filename), 404)
        self.get_adapter.assert_not_awaited()
        self.fetch.assert_not_awaited()

    def test_refresh_preserves_metadata_and_synchronizes_aliases(self):
        self.adapter.data[('antigravity', self.filename)]['access_token'] = 'stale-alias'
        with self.refresh_patch():
            self.assertEqual(self.get().status_code, 200)
        saved = self.adapter.data[('antigravity', self.filename)]
        for field in ('scopes', 'token_uri', 'project_id', 'metadata', 'disabled',
                      'client_id', 'client_secret'):
            self.assertEqual(saved[field], self.data[field])
        self.assertEqual(saved['token'], 'synthetic-new-token')
        self.assertEqual(saved['access_token'], 'synthetic-new-token')
        self.assertEqual(saved['refresh_token'], 'synthetic-new-refresh')
        self.assertEqual(saved['expiry'], '2099-02-01T00:00:00+00:00')
        self.fetch.assert_awaited_once_with('synthetic-new-token')
        self.adapter.update_credential_state.assert_not_awaited()

    def test_unexpired_token_never_writes_or_changes_disabled_state(self):
        original = deepcopy(self.adapter.data)
        self.assertEqual(self.get().status_code, 200)
        self.adapter.store_credential.assert_not_awaited()
        self.adapter.update_credential_state.assert_not_awaited()
        self.assertEqual(self.adapter.data, original)

    def test_access_token_only_is_supported(self):
        data = self.adapter.data[('antigravity', self.filename)]
        data['access_token'] = data.pop('token')
        self.assertEqual(self.get().status_code, 200)
        self.fetch.assert_awaited_once_with('synthetic-owner-token')
        self.adapter.store_credential.assert_not_awaited()

    def test_missing_credential_returns_unknown(self):
        self.adapter.data.pop(('antigravity', self.filename))
        self.assert_error(self.get(), 404)
        self.fetch.assert_not_awaited()

    def test_missing_token_never_reaches_upstream(self):
        self.adapter.data[('antigravity', self.filename)].pop('token')
        self.assert_error(self.get(), 502)
        self.fetch.assert_not_awaited()
        self.adapter.store_credential.assert_not_awaited()

    def test_expired_without_refresh_token_is_generic(self):
        data = self.adapter.data[('antigravity', self.filename)]
        data['expiry'] = '2000-01-01T00:00:00Z'
        data.pop('refresh_token')
        self.assert_error(self.get(), 502)
        self.fetch.assert_not_awaited()

    def test_refresh_failure_generic_and_private_logging(self):
        original = Mock()
        async def failed(credentials):
            quota.google_oauth_api.log.error('sensitive-upstream-detail')
            raise RuntimeError('sensitive-upstream-detail')
        with patch.object(quota.google_oauth_api, 'log', quota._PrivateQuotaLogger(original)), \
                patch.object(quota.Credentials, 'refresh_if_needed', failed):
            self.assert_error(self.get(), 502)
        original.error.assert_not_called()
        self.fetch.assert_not_awaited()
        self.adapter.store_credential.assert_not_awaited()

    def test_upstream_failures_and_invalid_models_generic(self):
        invalid = [None, {}, {'success': False, 'error': 'sensitive-upstream-detail'},
                   {'success': True, 'models': []},
                   {'success': True, 'models': {'m': None}},
                   {'success': True, 'models': {'m': {'remaining': 'sensitive-upstream-detail'}}},
                   {'success': True, 'models': {'m': {**self.models['synthetic-model'], 'remaining': True}}},
                   {'success': True, 'models': {'m': {**self.models['synthetic-model'], 'remaining': float('nan')}}},
                   {'success': True, 'models': {'m': {**self.models['synthetic-model'], 'resetTime': {}}}}]
        for result in invalid:
            with self.subTest(result=result):
                self.fetch.return_value = result
                self.assert_error(self.get(), 502)
        self.fetch.side_effect = RuntimeError('sensitive-upstream-detail')
        self.assert_error(self.get(), 502)

    def test_models_allowlist_strips_raw_upstream_and_credentials(self):
        self.fetch.return_value = {'success': True, 'error': 'sensitive-upstream-detail',
                                  'credentials': self.data,
                                  'models': {'synthetic-model': {**self.models['synthetic-model'],
                                                              'credentials': self.data,
                                                              'raw': 'sensitive-upstream-detail'}}}
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['models'], self.models)
        self.assertNotIn('sensitive-upstream-detail', response.text)
        self.assertNotIn('synthetic-secret', response.text)

    def test_empty_models_is_valid(self):
        self.fetch.return_value = {'success': True, 'models': {}}
        self.assertEqual(self.get().json()['models'], {})

    def test_storage_get_and_adapter_failures_generic(self):
        self.get_adapter.side_effect = RuntimeError('sensitive-upstream-detail')
        self.assert_error(self.get(), 503)
        self.get_adapter.side_effect = None
        self.adapter.get_credential.side_effect = RuntimeError('sensitive-upstream-detail')
        self.assert_error(self.get(), 503)
        self.fetch.assert_not_awaited()

    def test_storage_write_failure_false_or_exception(self):
        with self.refresh_patch():
            self.adapter.store_credential.return_value = False
            self.assert_error(self.get(), 503)
            self.adapter.store_credential.side_effect = RuntimeError('sensitive-upstream-detail')
            self.assert_error(self.get(), 503)
        self.fetch.assert_not_awaited()

    def test_ownership_storage_failure_generic(self):
        with patch.object(self.store, 'owned_filenames', side_effect=RuntimeError('sensitive-upstream-detail')):
            self.assert_error(self.get(), 503)
        self.get_adapter.assert_not_awaited()

    def test_deleted_during_refresh_never_recreated(self):
        def deleting(credentials):
            self.store.credential_state(self.owner, self.filename, 'antigravity', 'deleted')
            self.adapter.data.pop(('antigravity', self.filename))
            return self.refreshed(credentials)
        with self.refresh_patch(deleting):
            self.assert_error(self.get(), 404)
        self.adapter.store_credential.assert_not_awaited()
        self.fetch.assert_not_awaited()
        self.assertNotIn(('antigravity', self.filename), self.adapter.data)

    def test_deleted_during_upstream_returns_unknown(self):
        async def deleting(token):
            self.store.credential_state(self.owner, self.filename, 'antigravity', 'deleted')
            return {'success': True, 'models': self.models}
        self.fetch.side_effect = deleting
        self.assert_error(self.get(), 404)

    def test_deleted_during_failed_refresh_is_unknown(self):
        def deleting(credentials):
            self.store.credential_state(self.owner, self.filename, 'antigravity', 'deleted')
            raise RuntimeError('sensitive-upstream-detail')
        with self.refresh_patch(deleting):
            self.assert_error(self.get(), 404)
        self.adapter.store_credential.assert_not_awaited()
        self.fetch.assert_not_awaited()

    def test_disabled_expired_credential_can_refresh_without_state_updates(self):
        self.adapter.data[('antigravity', self.filename)]['expiry'] = '2000-01-01T00:00:00Z'
        async def refresh(credentials):
            self.refreshed(credentials)
        with patch.object(quota.Credentials, 'refresh', refresh):
            self.assertEqual(self.get().status_code, 200)
        self.assertTrue(self.adapter.data[('antigravity', self.filename)]['disabled'])
        self.adapter.update_credential_state.assert_not_awaited()
        self.fetch.assert_awaited_once_with('synthetic-new-token')

    def test_real_upstream_helper_error_logging_is_private(self):
        original = Mock()
        with patch.object(quota, 'fetch_quota_info', quota.antigravity.fetch_quota_info), \
                patch.object(quota.antigravity, 'get_antigravity_api_url',
                             AsyncMock(return_value='https://synthetic.invalid')), \
                patch.object(quota.antigravity, 'post_async',
                             AsyncMock(side_effect=RuntimeError('sensitive-upstream-detail'))), \
                patch.object(quota.antigravity, 'log', quota._PrivateQuotaLogger(original)):
            self.assert_error(self.get(), 502)
        original.error.assert_not_called()

    def test_private_logging_is_context_local(self):
        original = Mock()
        logger = quota._PrivateQuotaLogger(original)
        logger.error('outside-before')
        token = quota._private_quota.set(True)
        try:
            logger.error('sensitive-upstream-detail')
        finally:
            quota._private_quota.reset(token)
        logger.error('outside-after')
        self.assertEqual(original.error.call_count, 2)


if __name__ == '__main__':
    unittest.main()
