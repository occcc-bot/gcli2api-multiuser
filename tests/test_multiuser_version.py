"""Fork metadata/update regressions; the upstream version router stays unchanged."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from no_network import forbid_network
from src.multiuser.version import router

BASELINE = '6cc45620f1ecbe12bda7792b2daf8940d8a4ffde'


class ForkVersionTests(unittest.TestCase):
    def setUp(self):
        forbid_network(self)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_identifies_fork_and_new_upstream_baseline(self):
        with patch('src.httpx_client.get_async', AsyncMock()) as fetch:
            data = self.client.get('/version/info').json()
        self.assertTrue(data['success'])
        self.assertEqual(data['repository'], 'occcc-bot/gcli2api-multiuser')
        self.assertEqual(data['release'], '0.2.0')
        self.assertEqual(data['version'], 'multiuser-0.2.0')
        self.assertEqual(data['upstream_commit'], BASELINE)
        self.assertEqual(data['full_hash'], BASELINE)
        fetch.assert_not_awaited()

    def test_update_check_targets_fork_json_and_orders_semantic_release(self):
        for release, expected in [('0.1.0', False), ('0.2.0', False), ('0.3.0', True),
                                  ('0.10.0', True), ('1.0.0', True)]:
            with self.subTest(release=release):
                response = SimpleNamespace(status_code=200, text=json.dumps({
                    'release': release, 'upstream_commit': 'synthetic-upstream',
                    'message': 'synthetic release'}))
                with patch('src.httpx_client.get_async', AsyncMock(return_value=response)) as fetch:
                    data = self.client.get('/version/info?check_update=true').json()
                self.assertTrue(data['success'])
                self.assertTrue(data['check_update'])
                self.assertEqual(data['has_update'], expected)
                self.assertEqual(data['latest_version'], 'multiuser-' + release)
                self.assertEqual(data['latest_hash'], 'synthetic-upstream')
                self.assertEqual(data['latest_message'], 'synthetic release')
                fetch.assert_awaited_once_with(
                    'https://raw.githubusercontent.com/occcc-bot/gcli2api-multiuser/refs/heads/main/fork-version.json',
                    timeout=10.0)

    def test_upstream_version_route_cannot_shadow_installed_fork_route(self):
        from src.multiuser import install
        from src.panel.version import router as upstream_router
        app = FastAPI()
        install(app)
        app.include_router(upstream_router)
        with TestClient(app) as client:
            data = client.get('/version/info').json()
        self.assertEqual(data['repository'], 'occcc-bot/gcli2api-multiuser')
        self.assertEqual(data['release'], '0.2.0')

    def assert_update_failure(self, fetch):
        with patch('src.httpx_client.get_async', fetch):
            response = self.client.get('/version/info?check_update=true')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['release'], '0.2.0')
        self.assertFalse(data['check_update'])
        self.assertIsInstance(data['update_error'], str)
        self.assertNotIn('has_update', data)
        self.assertNotIn('sensitive-exception-detail', response.text)

    def test_update_failed_http_and_transport_keep_local_metadata_available(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                self.assert_update_failure(AsyncMock(return_value=SimpleNamespace(status_code=status, text='')))
        self.assert_update_failure(AsyncMock(side_effect=RuntimeError('sensitive-exception-detail')))

    def test_invalid_remote_json_or_release_never_claims_an_update(self):
        invalid = ['not-json', '[]', 'null', '{}']
        invalid += [json.dumps({'release': value}) for value in
                    (None, '', 2, [], {}, '0.3', '0.3.0.1', '0.3.x', '-1.2.3', 'v0.3.0')]
        for text in invalid:
            with self.subTest(text=text):
                self.assert_update_failure(AsyncMock(return_value=SimpleNamespace(status_code=200, text=text)))


if __name__ == '__main__':
    unittest.main()
