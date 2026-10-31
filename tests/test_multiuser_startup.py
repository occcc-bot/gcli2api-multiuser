"""Read-only startup preflight and deployment OAuth configuration regressions."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import AsyncMock, Mock, patch

from no_network import forbid_network
from src.multiuser import startup


class StartupPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        forbid_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'credentials.db'
        env = patch.dict(os.environ, {
            'CREDENTIALS_DIR': temp.name,
            'MULTIUSER_DB_PATH': str(self.root / 'members.sqlite3'),
            'POSTGRESQL_URI': '', 'WORKERS': '1', 'WEB_CONCURRENCY': '1',
        })
        env.start()
        self.addCleanup(env.stop)

    def legacy_database(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE antigravity_credentials(
                    filename TEXT PRIMARY KEY, credential_data TEXT NOT NULL);
                INSERT INTO antigravity_credentials VALUES(
                    'legacy-member.json', '{"refresh_token":"synthetic-must-survive"}');
            ''')
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def assert_legacy_unchanged(self, expected_hash):
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), expected_hash)
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute('SELECT filename,credential_data FROM antigravity_credentials').fetchall(),
                             [('legacy-member.json', '{"refresh_token":"synthetic-must-survive"}')])
            self.assertIn('filename', {row[1] for row in db.execute('PRAGMA table_info(antigravity_credentials)')})
            self.assertNotIn('email', {row[1] for row in db.execute('PRAGMA table_info(antigravity_credentials)')})

    async def test_real_lifespan_refuses_legacy_before_config_or_upstream_drop(self):
        expected_hash = self.legacy_database()
        import web
        init_config, initialize = AsyncMock(), AsyncMock()
        create_tables = Mock(side_effect=AssertionError('Upstream schema initialization must not run'))
        with patch('config.init_config', init_config), \
                patch('src.storage.database.Database.initialize', initialize), \
                patch('src.storage.database.Database._create_tables', create_tables):
            with self.assertRaisesRegex(RuntimeError, 'Legacy|legacy'):
                async with web.lifespan(web.app):
                    self.fail('Legacy database startup must not yield a running app')
        init_config.assert_not_awaited()
        initialize.assert_not_awaited()
        create_tables.assert_not_called()
        self.assert_legacy_unchanged(expected_hash)
        self.assertFalse((self.root / 'members.sqlite3').exists())

    async def test_invalid_worker_counts_refuse_before_any_database_inspection(self):
        expected_hash = self.legacy_database()
        for name in ('WORKERS', 'WEB_CONCURRENCY'):
            for value in ('2', '0', '-1', 'invalid'):
                with self.subTest(variable=name, value=value):
                    with patch.dict(os.environ, {name: value}), \
                            patch.object(startup, 'check_database_schema') as schema:
                        with self.assertRaisesRegex(RuntimeError, name):
                            await startup.preflight()
                        schema.assert_not_called()
                    self.assert_legacy_unchanged(expected_hash)

    async def test_preflight_accepts_new_and_email_databases_without_creating_or_mutating_credentials(self):
        lease = await startup.preflight()
        try:
            self.assertFalse(self.path.exists())
            self.assertFalse((self.root / 'members.sqlite3').exists())
            self.assertEqual({item.name for item in self.root.iterdir()}, {'multiuser-runtime.lock'})
            self.assertEqual((self.root / 'multiuser-runtime.lock').stat().st_mode & 0o777, 0o600)
            # Explicit worker settings are not the only enforcement: a second
            # process/lease on the same credential directory must also fail.
            with self.assertRaisesRegex(RuntimeError, 'runtime lock|one service process'):
                await startup.preflight()
        finally:
            lease.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE antigravity_credentials(email TEXT PRIMARY KEY, access_token TEXT);
                INSERT INTO antigravity_credentials VALUES('member@example.test','synthetic-token');
            ''')
        expected_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        lease = await startup.preflight()
        try:
            self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), expected_hash)
        finally:
            lease.close()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute('SELECT * FROM antigravity_credentials').fetchall(),
                             [('member@example.test', 'synthetic-token')])


class OAuthEnvironmentTests(unittest.TestCase):
    def setUp(self):
        forbid_network(self)

    def test_oauth_environment_requires_a_complete_pair_without_defaults(self):
        from src.multiuser import oauth
        defaults = {'ANTIGRAVITY_OAUTH_CLIENT_ID': '', 'ANTIGRAVITY_OAUTH_CLIENT_SECRET': ''}
        with patch.dict(os.environ, defaults):
            with self.assertRaisesRegex(ValueError, 'not configured'):
                oauth._client_config()
            with self.assertRaisesRegex(ValueError, 'not configured'):
                oauth._flow(oauth.REDIRECT_URI)
        for name in defaults:
            with self.subTest(configured=name):
                with patch.dict(os.environ, {**defaults, name: 'synthetic-partial-value'}):
                    with self.assertRaisesRegex(ValueError, 'Both|both'):
                        oauth._client_config()
                    with self.assertRaises(ValueError):
                        oauth._flow(oauth.REDIRECT_URI)

    def test_environment_reaches_real_flow_and_code_exchange_in_fresh_process(self):
        # Fresh import, not monkeypatching exported constants: this catches the
        # deployment bug where documented env settings never affected the app.
        tests = Path(__file__).resolve().parent
        code = textwrap.dedent(f'''
            import sys
            sys.path.insert(0, {str(tests)!r})
            import no_network
            import asyncio
            import httpx
            from unittest.mock import AsyncMock, patch
            from urllib.parse import parse_qs, urlsplit
            from src import utils, google_oauth_api
            from src.multiuser import oauth

            assert utils.ANTIGRAVITY_CLIENT_ID == 'synthetic-environment-client'
            assert utils.ANTIGRAVITY_CLIENT_SECRET == 'synthetic-environment-secret'
            assert oauth._client_config() == ('synthetic-environment-client', 'synthetic-environment-secret')
            flow = oauth._flow(oauth.REDIRECT_URI)
            assert flow.client_id == 'synthetic-environment-client'
            assert flow.client_secret == 'synthetic-environment-secret'
            query = parse_qs(urlsplit(flow.get_auth_url(state='synthetic-state')).query)
            assert query['client_id'] == ['synthetic-environment-client']
            response = httpx.Response(200, json={{'access_token': 'synthetic-access',
                'refresh_token': 'synthetic-refresh', 'expires_in': 3600}},
                request=httpx.Request('POST', 'https://never-called.invalid/token'))
            transport = AsyncMock(return_value=response)
            with patch.object(google_oauth_api, 'post_async', transport), \\
                    patch.object(google_oauth_api, 'get_oauth_proxy_url',
                                 AsyncMock(return_value='https://never-called.invalid')):
                asyncio.run(flow.exchange_code('synthetic-code'))
            payload = transport.await_args.kwargs['data']
            assert payload['client_id'] == 'synthetic-environment-client'
            assert payload['client_secret'] == 'synthetic-environment-secret'
            print('environment configuration applied')
        ''')
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, ANTIGRAVITY_OAUTH_CLIENT_ID='synthetic-environment-client',
                       ANTIGRAVITY_OAUTH_CLIENT_SECRET='synthetic-environment-secret',
                       CREDENTIALS_DIR=temp, MULTIUSER_DB_PATH=str(Path(temp) / 'members.sqlite3'),
                       POSTGRESQL_URI='', WORKERS='1', WEB_CONCURRENCY='1')
            result = subprocess.run([sys.executable, '-c', code], cwd=tests.parent,
                                    env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('environment configuration applied', result.stdout)


    def test_unconfigured_cold_import_keeps_existing_credential_refresh_working(self):
        tests = Path(__file__).resolve().parent
        code = textwrap.dedent(f'''
            import sys
            sys.path.insert(0, {str(tests)!r})
            import no_network
            import asyncio
            import httpx
            from unittest.mock import AsyncMock, patch
            from src import utils, google_oauth_api
            assert utils.ANTIGRAVITY_CLIENT_ID == ''
            assert utils.ANTIGRAVITY_CLIENT_SECRET == ''
            credentials = google_oauth_api.Credentials.from_dict({{
                'token': 'synthetic-old-access', 'refresh_token': 'synthetic-refresh',
                'client_id': 'synthetic-saved-client', 'client_secret': 'synthetic-saved-secret',
            }})
            response = httpx.Response(200, json={{'access_token': 'synthetic-new-access',
                'expires_in': 3600}}, request=httpx.Request('POST', 'https://never-called.invalid/token'))
            transport = AsyncMock(return_value=response)
            with patch.object(google_oauth_api, 'post_async', transport), \\
                    patch.object(google_oauth_api, 'get_oauth_proxy_url',
                                 AsyncMock(return_value='https://never-called.invalid')):
                assert asyncio.run(credentials.refresh_if_needed()) is True
            payload = transport.await_args.kwargs['data']
            assert payload['client_id'] == 'synthetic-saved-client'
            assert payload['client_secret'] == 'synthetic-saved-secret'
            assert credentials.access_token == 'synthetic-new-access'
            print('existing credential refreshed without OAuth configuration')
        ''')
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, CREDENTIALS_DIR=temp, POSTGRESQL_URI='')
            for name in ('ANTIGRAVITY_OAUTH_CLIENT_ID', 'ANTIGRAVITY_OAUTH_CLIENT_SECRET'):
                env.pop(name, None)
            result = subprocess.run([sys.executable, '-c', code], cwd=tests.parent,
                                    env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('existing credential refreshed without OAuth configuration', result.stdout)


class ColdImportTests(unittest.TestCase):
    def setUp(self):
        forbid_network(self)

    def test_database_and_web_cold_import_in_both_orders_without_circular_dependencies(self):
        tests = Path(__file__).resolve().parent
        for order in (('src.storage.database', 'web'), ('web', 'src.storage.database')):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as temp:
                code = textwrap.dedent(f'''
                    import sys
                    sys.path.insert(0, {str(tests)!r})
                    import no_network
                    import importlib
                    for module in {order!r}:
                        importlib.import_module(module)
                    from src.storage.database import Database
                    from src.multiuser.storage import get_storage_adapter
                    assert Database is not None and callable(get_storage_adapter)
                    print('cold imports succeeded')
                ''')
                env = dict(os.environ, CREDENTIALS_DIR=temp,
                           MULTIUSER_DB_PATH=str(Path(temp) / 'members.sqlite3'),
                           POSTGRESQL_URI='', WORKERS='1', WEB_CONCURRENCY='1')
                result = subprocess.run([sys.executable, '-c', code], cwd=tests.parent,
                                        env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('cold imports succeeded', result.stdout)


if __name__ == '__main__':
    unittest.main()
