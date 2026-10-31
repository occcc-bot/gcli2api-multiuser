"""Offline migration invariants; no application imports and no network access."""

from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'migrate_legacy.py'
spec = importlib.util.spec_from_file_location('migrate_legacy', SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

ACCOUNT_SCHEMA = '''
CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE, password_salt TEXT,
 password_hash TEXT, created_at INTEGER, disabled INTEGER DEFAULT 0);
CREATE TABLE api_keys (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE,
 user_id TEXT REFERENCES users(id), name TEXT, created_at INTEGER, expires_at INTEGER, revoked_at INTEGER);
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id),
 created_at INTEGER, expires_at INTEGER, revoked_at INTEGER);
CREATE TABLE invites (token_hash TEXT PRIMARY KEY, created_at INTEGER,
 expires_at INTEGER, used_at INTEGER, user_id TEXT REFERENCES users(id));
CREATE TABLE credential_owners (mode TEXT, filename TEXT, user_id TEXT REFERENCES users(id),
 created_at INTEGER, state TEXT DEFAULT 'pending', PRIMARY KEY(mode,filename));
CREATE TABLE member_oauth_flows (state TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id),
 redirect_uri TEXT, created_at INTEGER, expires_at INTEGER, status TEXT DEFAULT 'pending');
'''
LEGACY_SCHEMA = '''
CREATE TABLE antigravity_credentials (id INTEGER PRIMARY KEY, filename TEXT UNIQUE NOT NULL,
 credential_data TEXT NOT NULL, disabled INTEGER DEFAULT 0, error_codes TEXT DEFAULT '[]',
 last_success REAL, user_email TEXT, model_cooldowns TEXT DEFAULT '{}', rotation_order INTEGER,
 call_count INTEGER, created_at REAL, updated_at REAL, error_messages TEXT DEFAULT '[]',
 tier TEXT DEFAULT 'pro', enable_credit INTEGER DEFAULT 0);
CREATE TABLE credentials (filename TEXT, credential_data TEXT);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL);
'''


def digest_tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'old'
        self.source.mkdir()
        self.target = self.root / 'new'
        self.accounts = self.source / 'multiuser.sqlite3'
        self.credentials = self.source / 'credentials.db'
        with closing(sqlite3.connect(self.accounts)) as db, db:
            db.executescript(ACCOUNT_SCHEMA)
            for user in ('u1', 'u2'):
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',
                           (user, 'user-' + user, 'salt-' + user, 'hash-' + user, 100, 0))
            db.execute('INSERT INTO api_keys VALUES(?,?,?,?,?,?,?)',
                       ('k1', 'secret-key-hash', 'u1', 'main', 100, 9999999999, None))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                       ('session-hash', 'u1', 100, 9999999999, None))
            db.execute('INSERT INTO invites VALUES(?,?,?,?,?)',
                       ('invite-hash', 100, 9999999999, None, None))
            db.execute('INSERT INTO member_oauth_flows VALUES(?,?,?,?,?,?)',
                       ('pending-flow', 'u1', 'http://localhost/callback', 100, 9999999999, 'pending'))
        with closing(sqlite3.connect(self.credentials)) as db:
            db.executescript(LEGACY_SCHEMA)

    def add(self, name, email='member@example.test', *, owner='u1', state='active',
            disabled=0, refresh=None, updated=200, cooldowns=None, data_extra=None):
        data = {'access_token': 'PRIVATE-ACCESS-' + name, 'token': 'OLD-TOKEN',
                'refresh_token': refresh or 'PRIVATE-REFRESH-' + name, 'client_id': 'client',
                'client_secret': 'PRIVATE-CLIENT', 'project_id': 'project-12345',
                'expiry': '2027-01-01T00:00:00+00:00', 'enable_credit': True}
        data.update(data_extra or {})
        with closing(sqlite3.connect(self.credentials)) as db, db:
            db.execute('''INSERT INTO antigravity_credentials
                (filename,credential_data,disabled,user_email,error_codes,error_messages,
                 last_success,model_cooldowns,created_at,updated_at,tier)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       (name, json.dumps(data), disabled, email, '[429]', '{"429":"private error"}',
                        150, json.dumps(cooldowns or {}), 100, updated, 'pro'))
        if owner is not None:
            with closing(sqlite3.connect(self.accounts)) as db, db:
                db.execute('INSERT INTO credential_owners VALUES(?,?,?,?,?)',
                           ('antigravity', name, owner, 100, state))

    def migrate(self, **kwargs):
        return migration.migrate(self.source, self.target, **kwargs)

    def rows(self, database, table):
        with closing(sqlite3.connect(database)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]

    def test_accounts_credentials_config_and_archive(self):
        self.add('mu_member.json', ' Member@Example.Test ', disabled=1,
                 cooldowns={'claude-sonnet': 900, 'claude-opus': 950, 'gemini-3-pro': 800})
        self.add('admin.json', 'admin@example.test', owner=None)
        with closing(sqlite3.connect(self.credentials)) as db, db:
            db.execute('INSERT INTO credentials VALUES(?,?)', ('old-cli.json', '{"refresh_token":"CLI-SECRET"}'))
            db.execute('INSERT INTO config VALUES(?,?,?)', ('api_password', '"PRIVATE-PASSWORD"', 123))
            db.execute('INSERT INTO config VALUES(?,?,?)', ('credentials_dir', '"/old/creds"', 123))
            db.execute('INSERT INTO config VALUES(?,?,?)', ('geminicli_api_url', '"legacy"', 123))
        (self.source / 'creds.toml').write_text('["old-cli.json"]\nrefresh_token = "PRIVATE-TOML"\n')
        before = digest_tree(self.source)
        original = {table: self.rows(self.accounts, table) for table in ('users', 'api_keys', 'sessions', 'invites')}
        report = self.migrate()
        self.assertEqual(before, digest_tree(self.source))
        self.assertEqual(report['credentials_migrated'], 2)
        self.assertEqual(report['credentials_disabled'], 1)
        self.assertEqual(report['geminicli_archived'], 1)
        self.assertEqual(report['legacy_toml_archived'], 1)
        self.assertEqual(report['configs_migrated'], 1)
        self.assertEqual(report['configs_skipped'], 2)
        for table, expected in original.items():
            self.assertEqual(self.rows(self.target / 'multiuser.sqlite3', table), expected)
        owners = self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')
        self.assertEqual(owners[0]['filename'], 'member@example.test')
        live = self.rows(self.target / 'credentials.db', 'antigravity_credentials')
        member = next(r for r in live if r['email'] == 'member@example.test')
        self.assertEqual(member['access_token'], 'PRIVATE-ACCESS-mu_member.json')
        self.assertEqual(member['refresh_token'], 'PRIVATE-REFRESH-mu_member.json')
        self.assertEqual(member['disabled'], 1)
        self.assertEqual(json.loads(member['model_cooldowns']), {'claude_gpt': 950, 'gemini': 800})
        with closing(sqlite3.connect(self.target / 'credentials.db')) as db:
            self.assertNotIn('credentials', migration._tables(db))
        self.assertEqual(self.rows(self.target / 'credentials.db', 'config')[0]['key'], 'api_password')
        self.assertEqual(self.rows(self.target / 'multiuser.sqlite3', 'member_oauth_flows')[0]['status'], 'consumed')
        self.assertEqual((self.target / 'legacy_archive' / 'creds.toml').read_bytes(),
                         (self.source / 'creds.toml').read_bytes())
        for path in self.target.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertTrue(all(type(v) is int for v in report.values()))
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_dry_run_then_publish_then_verified_noop(self):
        self.add('mu_1.json')
        before = digest_tree(self.source)
        report = self.migrate(dry_run=True)
        self.assertEqual(report['credentials_migrated'], 1)
        self.assertFalse(self.target.exists())
        self.assertEqual(before, digest_tree(self.source))
        self.migrate()
        target_before = digest_tree(self.target)
        repeat = self.migrate()
        self.assertEqual(repeat['already_migrated'], 1)
        self.assertEqual(target_before, digest_tree(self.target))
        self.assertEqual(before, digest_tree(self.source))

    def test_nonempty_or_changed_target_is_never_overwritten(self):
        self.add('mu_1.json')
        self.target.mkdir()
        (self.target / 'important').write_text('do not replace')
        with self.assertRaises(migration.MigrationError):
            self.migrate()
        self.assertEqual((self.target / 'important').read_text(), 'do not replace')
        (self.target / 'important').unlink()
        self.migrate()
        with closing(sqlite3.connect(self.target / 'multiuser.sqlite3')) as db, db:
            db.execute("UPDATE users SET password_hash='changed'")
        before = digest_tree(self.target)
        with self.assertRaises(migration.MigrationError):
            self.migrate()
        self.assertEqual(before, digest_tree(self.target))

    def test_pending_deleted_and_unmapped_mu_never_become_admin(self):
        self.add('mu_pending.json', 'pending@example.test', state='pending')
        self.add('mu_deleted.json', 'deleted@example.test', state='deleted')
        self.add('mu_orphan.json', 'orphan@example.test', owner=None)
        self.add('legacy-admin.json', 'admin@example.test', owner=None)
        report = self.migrate()
        self.assertEqual(report['orphan_quarantined'], 1)
        self.assertEqual(report['inactive_credentials'], 2)
        live = {r['email']: r for r in self.rows(self.target / 'credentials.db', 'antigravity_credentials')}
        self.assertNotIn('orphan@example.test', live)
        self.assertEqual(live['pending@example.test']['disabled'], 1)
        self.assertEqual(live['deleted@example.test']['disabled'], 1)
        self.assertEqual(live['admin@example.test']['disabled'], 0)
        owners = {r['filename']: r['state'] for r in self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')}
        self.assertEqual(owners, {'pending@example.test': 'pending', 'deleted@example.test': 'deleted'})

    def test_synthetic_email_is_stable_and_preserves_ownership(self):
        self.add('mu_missing.json', None)
        self.add('legacy-admin.json', '')
        report = self.migrate()
        self.assertEqual(report['synthetic_emails'], 2)
        expected = 'legacy-' + hashlib.sha256(b'mu_missing.json').hexdigest()[:24] + '@migration.invalid'
        self.assertEqual(self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')[0]['filename'], expected)
        self.assertIn(expected, [r['email'] for r in self.rows(self.target / 'credentials.db', 'antigravity_credentials')])

    def test_duplicate_email_cross_owner_fails_whole_migration(self):
        for second_owner in ('u2', None):
            with self.subTest(second_owner=second_owner):
                self.add('mu_1.json', 'same@example.test')
                self.add('second.json', ' SAME@EXAMPLE.TEST ', owner=second_owner)
                before = digest_tree(self.source)
                with self.assertRaises(migration.MigrationError) as caught:
                    self.migrate()
                self.assertEqual(caught.exception.reason, 'ownership_conflict')
                self.assertEqual(caught.exception.counts['ownership_conflicts'], 1)
                self.assertFalse(self.target.exists())
                self.assertEqual(before, digest_tree(self.source))
                with closing(sqlite3.connect(self.credentials)) as db, db:
                    db.execute('DELETE FROM antigravity_credentials')
                with closing(sqlite3.connect(self.accounts)) as db, db:
                    db.execute('DELETE FROM credential_owners')

    def test_duplicate_refresh_token_cross_owner_even_without_email_fails(self):
        self.add('mu_1.json', None, refresh='SHARED-TOKEN')
        self.add('admin.json', 'different@example.test', owner=None, refresh='SHARED-TOKEN')
        with self.assertRaises(migration.MigrationError) as caught:
            self.migrate()
        self.assertEqual(caught.exception.reason, 'ownership_conflict')
        self.assertFalse(self.target.exists())
        self.assertNotIn('SHARED-TOKEN', str(caught.exception))

    def test_orphan_conflicts_cannot_be_implicitly_given_to_admin(self):
        self.add('mu_orphan.json', 'same@example.test', owner=None)
        self.add('admin.json', 'same@example.test', owner=None)
        with self.assertRaises(migration.MigrationError):
            self.migrate()
        self.assertFalse(self.target.exists())

    def test_same_owner_duplicate_merges_restrictively(self):
        self.add('mu_old.json', 'same@example.test', state='deleted', updated=200,
                 cooldowns={'claude-opus': 900})
        self.add('mu_new.json', 'same@example.test', state='active', updated=300,
                 cooldowns={'gemini-pro': 800})
        report = self.migrate()
        self.assertEqual(report['duplicate_credentials'], 1)
        live = self.rows(self.target / 'credentials.db', 'antigravity_credentials')
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]['access_token'], 'PRIVATE-ACCESS-mu_new.json')
        self.assertEqual(live[0]['disabled'], 1)
        self.assertEqual(json.loads(live[0]['model_cooldowns']), {'claude_gpt': 900, 'gemini': 800})
        owners = self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0]['state'], 'deleted')

    def test_unresolved_ownership_downgraded_not_deleted(self):
        self.add('mu_1.json')
        with closing(sqlite3.connect(self.accounts)) as db, db:
            db.execute('INSERT INTO credential_owners VALUES(?,?,?,?,?)',
                       ('antigravity', 'mu_missing.json', 'u1', 100, 'active'))
            db.execute('INSERT INTO credential_owners VALUES(?,?,?,?,?)',
                       ('geminicli', 'cli.json', 'u1', 100, 'active'))
        report = self.migrate()
        self.assertEqual(report['ownership_rows_unresolved'], 1)
        owners = {r['filename']: r['state'] for r in self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')}
        self.assertEqual(owners['mu_missing.json'], 'pending')
        self.assertEqual(owners['cli.json'], 'pending')

    def test_sqlite_wal_snapshots_include_committed_rows_without_source_writes(self):
        self.add('mu_existing.json')
        with closing(sqlite3.connect(self.credentials)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("UPDATE antigravity_credentials SET disabled=1")
            db.commit()
            self.assertGreater(Path(str(self.credentials) + '-wal').stat().st_size, 0)
            before = digest_tree(self.source)
            self.migrate()
            self.assertEqual(before, digest_tree(self.source))
            self.assertEqual(self.rows(self.target / 'credentials.db', 'antigravity_credentials')[0]['disabled'], 1)

    def test_source_change_prevents_publication(self):
        self.add('mu_1.json')
        verify = migration._verify_source
        calls = []

        def changed(observed):
            calls.append(1)
            if len(calls) == 2:
                with closing(sqlite3.connect(self.credentials)) as db, db:
                    db.execute('INSERT INTO config VALUES(?,?,?)', ('proxy', '"changed"', 10))
            verify(observed)

        with patch.object(migration, '_verify_source', side_effect=changed):
            with self.assertRaises(migration.MigrationError) as caught:
                self.migrate()
        self.assertEqual(caught.exception.reason, 'source_changed')
        self.assertFalse(self.target.exists())

    def test_bad_schema_bad_json_and_overlap_leave_source_untouched(self):
        self.add('mu_1.json')
        with closing(sqlite3.connect(self.credentials)) as db, db:
            db.execute("UPDATE antigravity_credentials SET credential_data='PRIVATE-invalid-json'")
        before = digest_tree(self.source)
        with self.assertRaises(migration.MigrationError) as caught:
            self.migrate()
        self.assertEqual(caught.exception.reason, 'invalid_json')
        self.assertEqual(before, digest_tree(self.source))
        self.assertFalse(self.target.exists())
        for target in (self.source, self.source / 'nested'):
            with self.assertRaises(migration.MigrationError):
                migration.migrate(self.source, target)

    def test_cli_reports_counts_only_and_never_overwrites_report(self):
        self.add('mu_1.json', 'PRIVATE-MAIL@example.test')
        report = self.root / 'report.json'
        command = [sys.executable, str(SCRIPT), '--source', str(self.source), '--target', str(self.target),
                   '--dry-run', '--report', str(report)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        payload = json.loads(result.stdout)
        self.assertTrue(all(type(value) is int for value in payload.values()))
        self.assertNotIn('PRIVATE', result.stdout + result.stderr)
        self.assertEqual(json.loads(report.read_text()), payload)
        original = report.read_bytes()
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(report.read_bytes(), original)
        self.assertFalse(self.target.exists())

    def test_private_email_map_fills_missing_and_keeps_synthetic_fallback(self):
        self.add('mu_mapped.json', None)
        self.add('mu_synthetic.json', None, owner='u2')
        mapping = self.root / 'email-map.json'
        mapping.write_text(json.dumps({'mu_mapped.json': ' Resolved@Example.Test '}))
        original = mapping.read_bytes()
        report = self.migrate(email_map=mapping)
        self.assertEqual(report['mapped_emails'], 1)
        self.assertEqual(report['synthetic_emails'], 1)
        self.assertEqual(original, mapping.read_bytes())
        owners = self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')
        mapped = next(row for row in owners if row['filename'] == 'resolved@example.test')
        self.assertEqual(mapped['user_id'], 'u1')
        synthetic = next(row for row in owners if row['filename'].endswith('@migration.invalid'))
        self.assertEqual(synthetic['user_id'], 'u2')
        self.assertEqual(synthetic['state'], 'active')
        live = self.rows(self.target / 'credentials.db', 'antigravity_credentials')
        self.assertTrue(all(row['disabled'] == 0 for row in live))

    def test_private_email_map_validates_identity_and_conflicts(self):
        self.add('mu_1.json', None)
        self.add('mu_2.json', 'other@example.test', owner='u2')
        mapping = self.root / 'email-map.json'
        for content in ({'mu_1.json': ' OTHER@EXAMPLE.TEST '},
                        {'mu_2.json': 'different@example.test'},
                        {'unknown.json': 'unused@example.test'},
                        {'mu_1.json': 'not-an-email'},
                        {'mu_1.json': 'path/name@example.test'},
                        {'mu_1.json': 'percent%name@example.test'},
                        {'mu_1.json': '非ascii@example.test'},
                        {'../mu_1.json': 'valid@example.test'},
                        {'mu_1.json': None}):
            with self.subTest(content=content):
                mapping.write_text(json.dumps(content))
                before = digest_tree(self.source)
                with self.assertRaises(migration.MigrationError):
                    self.migrate(email_map=mapping)
                self.assertFalse(self.target.exists())
                self.assertEqual(before, digest_tree(self.source))

    def test_duplicate_email_map_keys_fail_closed(self):
        self.add('mu_1.json', None)
        mapping = self.root / 'email-map.json'
        mapping.write_text('{"mu_1.json":"first@example.test","mu_1.json":"second@example.test"}')
        with self.assertRaises(migration.MigrationError) as caught:
            self.migrate(email_map=mapping)
        self.assertEqual(caught.exception.reason, 'duplicate_email_map_key')
        self.assertFalse(self.target.exists())

    def test_source_database_open_is_never_attempted_even_with_wal(self):
        self.add('mu_1.json')
        connect = sqlite3.connect
        calls = []

        def private_copy_only(path, *args, **kwargs):
            resolved = Path(path).resolve()
            self.assertNotIn(self.source.resolve(), resolved.parents)
            calls.append(resolved)
            return connect(path, *args, **kwargs)

        with patch.object(migration.sqlite3, 'connect', side_effect=private_copy_only):
            self.migrate()
        self.assertGreater(len(calls), 0)

    def test_null_disabled_state_does_not_become_enabled(self):
        self.add('mu_1.json', disabled=None)
        report = self.migrate()
        self.assertEqual(report['credentials_disabled'], 1)
        self.assertEqual(self.rows(self.target / 'credentials.db', 'antigravity_credentials')[0]['disabled'], 1)

    def test_cli_unusable_report_path_does_not_publish_target(self):
        self.add('mu_1.json')
        result = subprocess.run([sys.executable, str(SCRIPT), '--source', str(self.source),
                                 '--target', str(self.target), '--report', str(self.root / 'absent' / 'report.json')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['errors'], 1)
        self.assertFalse(self.target.exists())

    def test_preserve_collisions_keeps_every_source_record_and_its_owner(self):
        self.add('mu_a1.json', 'shared-a@example.test', owner='u1')
        self.add('mu_a2.json', 'SHARED-A@example.test', owner='u1', disabled=1)
        self.add('mu_b.json', 'shared-b@example.test', owner='u2')
        self.add('admin-b.json', 'shared-b@example.test', owner=None)
        self.add('mu_missing.json', None, owner='u1')
        self.add('mu_unique.json', 'unique@example.test', owner='u2')
        self.add('admin-unique.json', 'admin@example.test', owner=None)
        before = digest_tree(self.source)
        report = self.migrate(preserve_collisions=True, dry_run=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(report['credentials_migrated'], 7)
        self.assertEqual(report['aliased_collision_credentials'], 4)
        self.assertEqual(report['synthetic_emails'], 1)
        self.assertEqual(report['duplicate_credentials'], 0)
        self.migrate(preserve_collisions=True)
        self.assertEqual(before, digest_tree(self.source))
        live = {r['email']: r for r in self.rows(self.target / 'credentials.db', 'antigravity_credentials')}
        self.assertEqual(len(live), 7)
        for name in ('mu_a1.json', 'mu_a2.json', 'mu_b.json', 'admin-b.json', 'mu_missing.json'):
            record = live[migration._legacy_id(name)]
            self.assertEqual(record['refresh_token'], 'PRIVATE-REFRESH-' + name)
            self.assertEqual(record['access_token'], 'PRIVATE-ACCESS-' + name)
            self.assertEqual(record['disabled'], int(name == 'mu_a2.json'))
        self.assertIn('unique@example.test', live)
        self.assertIn('admin@example.test', live)
        self.assertNotIn('shared-a@example.test', live)
        self.assertNotIn('shared-b@example.test', live)
        owners = {r['filename']: r for r in self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')}
        self.assertEqual(len(owners), 5)
        self.assertEqual(owners[migration._legacy_id('mu_a1.json')]['user_id'], 'u1')
        self.assertEqual(owners[migration._legacy_id('mu_a2.json')]['user_id'], 'u1')
        self.assertEqual(owners[migration._legacy_id('mu_b.json')]['user_id'], 'u2')
        self.assertEqual(owners[migration._legacy_id('mu_missing.json')]['state'], 'active')
        self.assertNotIn(migration._legacy_id('admin-b.json'), owners)
        identity_path = self.target / 'legacy_archive' / 'identity-map.json'
        identities = json.loads(identity_path.read_text())
        self.assertEqual(len(identities), 4)
        self.assertEqual(identities['mu_b.json'], {
            'original_email': 'shared-b@example.test', 'database_id': migration._legacy_id('mu_b.json')})
        self.assertEqual(identity_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('PRIVATE-REFRESH', identity_path.read_text())
        target_before = digest_tree(self.target)
        self.assertEqual(self.migrate(preserve_collisions=True)['already_migrated'], 1)
        with self.assertRaises(migration.MigrationError):
            self.migrate()  # A different mode is NOT a verified no-op.
        self.assertEqual(target_before, digest_tree(self.target))

    def test_preserve_collisions_keeps_pending_deleted_and_active_independent(self):
        self.add('mu_pending.json', 'shared@example.test', state='pending')
        self.add('mu_deleted.json', 'shared@example.test', state='deleted')
        self.add('mu_active.json', 'shared@example.test', state='active')
        report = self.migrate(preserve_collisions=True)
        self.assertEqual(report['aliased_collision_credentials'], 3)
        owners = {r['filename']: r for r in self.rows(self.target / 'multiuser.sqlite3', 'credential_owners')}
        live = {r['email']: r for r in self.rows(self.target / 'credentials.db', 'antigravity_credentials')}
        for state in ('pending', 'deleted', 'active'):
            identity = migration._legacy_id('mu_' + state + '.json')
            self.assertEqual(owners[identity]['state'], state)
            self.assertEqual(live[identity]['disabled'], int(state != 'active'))
        self.assertEqual(len(owners), 3)
        self.assertEqual(len(live), 3)

    def test_preserve_collisions_never_exempts_cross_owner_refresh_conflict(self):
        self.add('mu_1.json', 'same@example.test', owner='u1', refresh='SHARED-PRIVATE-REFRESH')
        self.add('admin.json', 'same@example.test', owner=None, refresh='SHARED-PRIVATE-REFRESH')
        before = digest_tree(self.source)
        with self.assertRaises(migration.MigrationError) as caught:
            self.migrate(preserve_collisions=True)
        self.assertEqual(caught.exception.reason, 'ownership_conflict')
        self.assertEqual(caught.exception.counts['ownership_conflicts'], 1)
        self.assertFalse(self.target.exists())
        self.assertEqual(before, digest_tree(self.source))

    def test_cli_preserve_collisions_with_email_map_reports_only_counts(self):
        self.add('mu_1.json', None, owner='u1')
        self.add('admin.json', 'PRIVATE-SHARED@example.test', owner=None)
        mapping = self.root / 'private-email-map.json'
        mapping.write_text(json.dumps({'mu_1.json': 'private-shared@example.test'}))
        base = [sys.executable, str(SCRIPT), '--source', str(self.source), '--target', str(self.target),
                '--email-map', str(mapping), '--dry-run']
        strict = subprocess.run(base, capture_output=True, text=True)
        self.assertEqual(strict.returncode, 1)
        self.assertEqual(json.loads(strict.stdout)['ownership_conflicts'], 1)
        preserved = subprocess.run(base + ['--preserve-collisions'], capture_output=True, text=True)
        self.assertEqual(preserved.returncode, 0, preserved.stderr)
        report = json.loads(preserved.stdout)
        self.assertEqual(report['aliased_collision_credentials'], 2)
        self.assertEqual(report['mapped_emails'], 1)
        self.assertEqual(report['credentials_migrated'], 2)
        self.assertEqual(report['synthetic_emails'], 0)
        self.assertTrue(all(type(value) is int for value in report.values()))
        self.assertNotIn('PRIVATE', preserved.stdout + preserved.stderr)
        self.assertFalse(self.target.exists())

    def test_preserve_collisions_generated_id_cannot_merge_with_source_email(self):
        self.add('mu_first.json', 'shared@example.test')
        self.add('mu_second.json', 'shared@example.test')
        self.add('mu_reserved.json', migration._legacy_id('mu_first.json'))
        with self.assertRaises(migration.MigrationError) as caught:
            self.migrate(preserve_collisions=True)
        self.assertEqual(caught.exception.reason, 'migration_identity_collision')
        self.assertFalse(self.target.exists())

    def test_cli_error_does_not_leak_sqlite_or_path_details(self):
        self.add('mu_1.json')
        with closing(sqlite3.connect(self.credentials)) as db, db:
            db.execute("UPDATE antigravity_credentials SET credential_data='PRIVATE-JSON-ERROR'")
        result = subprocess.run([sys.executable, str(SCRIPT), '--source', str(self.source),
                                 '--target', str(self.target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout)['errors'], 1)
        self.assertNotIn(str(self.root), result.stdout)
        self.assertNotIn('PRIVATE', result.stdout)


if __name__ == '__main__':
    unittest.main()
