"""Fork release metadata and update checks; no external network calls."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.panel.version import router


class ForkVersionTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_identifies_fork_and_upstream_baseline(self):
        data = self.client.get('/version/info').json()
        self.assertTrue(data['success'])
        self.assertEqual(data['repository'], 'occcc-bot/gcli2api-multiuser')
        self.assertEqual(data['release'], '0.1.0')
        self.assertEqual(data['upstream_commit'], '934c8ce8b3648b2530ab8a36c409d70d699ab680')

    def test_update_check_targets_fork_and_compares_release(self):
        for release, expected in [('0.1.0', False), ('0.2.0', True)]:
            response = SimpleNamespace(status_code=200,
                                       text=f'release={release}\nshort_hash=multiuser-{release}\n')
            fetch = AsyncMock(return_value=response)
            with patch('src.httpx_client.get_async', fetch):
                data = self.client.get('/version/info?check_update=true').json()
            self.assertEqual(data['has_update'], expected)
            self.assertIn('occcc-bot/gcli2api-multiuser/refs/heads/main/', fetch.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
