#!/usr/bin/env python3
"""Offline, standard-library-only migration of a legacy local SQLite creds directory.

Stop the old application (including background writers) first. The source is never
opened with SQLite: DB/WAL files are copied using read-only file descriptors, then
SQLite backup() recovers a consistent snapshot in private scratch space. This also
avoids creating/updating WAL shared-memory sidecars in the source. Each source file
is verified again before publication; concurrent writers are an error, not supported.

Usage:
    python3 scripts/migrate_legacy.py --source /old/creds --target /new/creds --dry-run
    python3 scripts/migrate_legacy.py --source /old/creds --target /new/creds

The target must be absent or empty. Publication is a single directory rename. An
unchanged repeat is a verified no-op; a changed/nonempty target is never overwritten.
SQLite backups (including GeminiCLI records) remain under legacy_archive/, never
in the new live pool. credential_owners keeps its schema: filename now holds email
for migrated Antigravity rows. Pending/deleted mappings never become active.
Reports contain counts only; exceptions never include source data or SQLite text.
Use --email-map /private/map.json for a trusted {"old-filename.json":"email"}
object to fill absent identities. Its values use the same canonicalization as the
application, and conflicts with existing identities or owners abort the migration.
Remaining missing identities use legacy-<sha256(filename)[:24]>@migration.invalid;
this is a local ID only and does not disable or change the credential's ownership.
Use --preserve-collisions ONLY to preserve intentionally separate source records
with the same real email: EVERY filename in such a group receives its own stable
legacy-<sha256(filename)[:24]>@migration.invalid local ID, without merging tokens,
state, or ownership. A private legacy_archive/identity-map.json records original
emails and new IDs; reports expose only aliased_collision_credentials counts.
Cross-owner reuse of the SAME refresh_token is always rejected, even in this mode.
Default behavior is unchanged: cross-owner email collisions fail closed; same-owner
duplicates are conservatively merged. Changing this option cannot overwrite an
already migrated target. Environment-only settings/passwords must be carried over
separately by deployment.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile


VERSION = 2
MANIFEST = '.legacy-migration.json'
CONFIG_KEYS = frozenset('''proxy oauth_proxy_url googleapis_proxy_url antigravity_api_url
    auto_ban_enabled auto_ban_error_codes retry_429_max_retries retry_429_enabled
    retry_429_interval anti_truncation_max_attempts return_thoughts_to_frontend
    sensitive_words sensitive_words_enabled ip_whitelist ip_whitelist_enabled
    model_rewrite_enabled model_rewrite_rules usage_log_enabled request_log_enabled
    request_log_error_codes credential_select_mode host port api_password panel_password'''.split())
COUNTERS = '''users api_keys sessions invites ownership_rows credentials_seen
    credentials_migrated credentials_disabled missing_email_quarantined
    orphan_quarantined inactive_credentials duplicate_credentials geminicli_archived
    ownership_rows_remapped ownership_rows_unresolved configs_migrated configs_skipped
    sqlite_backups already_migrated errors ownership_conflicts synthetic_emails
    legacy_toml_archived mapped_emails aliased_collision_credentials'''.split()
NEW_SCHEMA = '''
CREATE TABLE antigravity_credentials (
 id INTEGER PRIMARY KEY AUTOINCREMENT, email VARCHAR(320) NOT NULL UNIQUE,
 access_token TEXT, refresh_token TEXT, client_id TEXT, client_secret TEXT,
 project_id TEXT, expiry TEXT, disabled BOOLEAN, error_codes TEXT,
 error_messages TEXT, last_success FLOAT, model_cooldowns TEXT,
 tier VARCHAR(32), created_at FLOAT, updated_at FLOAT
);
CREATE INDEX idx_ag_disabled ON antigravity_credentials(disabled);
CREATE TABLE config (key VARCHAR(255) PRIMARY KEY, value TEXT NOT NULL, updated_at FLOAT);
'''


class MigrationError(Exception):
    """Only static, non-sensitive error identifiers may be passed here."""

    def __init__(self, reason: str, counts: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.counts = dict(counts or {})
        self.counts['errors'] = 1


def _fail(reason: str, counts: dict | None = None):
    raise MigrationError(reason, counts)


def _json(value, default=None):
    if value is None or value == '':
        return default
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError, RecursionError):
        _fail('invalid_json')


def _email_map_json(raw):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail('duplicate_email_map_key')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError, RecursionError):
        _fail('invalid_email_map')


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path):
    if path.is_symlink() or not path.is_file():
        _fail('non_regular_source_file')


def _copy_readonly(source: Path, destination: Path) -> str:
    _regular(source)
    before = source.stat()
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    digest = hashlib.sha256()
    with os.fdopen(fd, 'rb') as src, destination.open('xb') as dst:
        if os.fstat(src.fileno()).st_ino != before.st_ino:
            _fail('source_changed')
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            dst.write(chunk)
            digest.update(chunk)
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns):
        _fail('source_changed')
    os.chmod(destination, 0o600)
    return digest.hexdigest()


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    # Table names are constants, never taken from credential contents.
    return {r[1] for r in db.execute(f'PRAGMA table_info({table})')}


def _backup(source: Path, output: Path, scratch: Path, observed: dict[Path, str]):
    """Recover copied WAL locally, then use the SQLite backup API, not file-copy DBs."""
    local = scratch / ('db-' + str(len(observed)))
    local.mkdir(mode=0o700)
    working = local / 'working.db'
    observed[source] = _copy_readonly(source, working)
    wal = Path(str(source) + '-wal')
    if wal.exists():
        observed[wal] = _copy_readonly(wal, Path(str(working) + '-wal'))
    else:
        observed[wal] = ''  # Detect newly created WAL files at verification time.
    journal = Path(str(source) + '-journal')
    if journal.exists() and journal.stat().st_size:
        _fail('source_has_rollback_journal')
    with closing(sqlite3.connect(working)) as src, closing(sqlite3.connect(output)) as dst:
        src.execute('PRAGMA trusted_schema=OFF')
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            _fail('invalid_sqlite_snapshot')
    os.chmod(output, 0o600)


def _verify_source(observed: dict[Path, str]):
    for path, digest in observed.items():
        if not digest:
            if path.exists():
                _fail('source_changed')
        else:
            _regular(path)
            if _hash(path) != digest:
                _fail('source_changed')


def _fingerprint(observed: dict[Path, str]) -> str:
    # Absolute paths affect identity but are never recorded in the manifest/report.
    text = json.dumps(sorted((str(p), digest) for p, digest in observed.items()))
    return hashlib.sha256(text.encode()).hexdigest()


def _connect(path: Path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA trusted_schema=OFF')
    return db


def _number(value, default=0):
    if value is None:
        return default
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail('invalid_numeric_state')
    return value


def _disabled(value):
    if value in (False, 0, '0'):
        return False
    if value in (None, '', True, 1, '1'):
        return True
    _fail('invalid_disabled_state')


def _email(row, data):
    candidates = [row.get('user_email'), row.get('email'), data.get('email'), data.get('user_email')]
    normalized = set()
    for value in candidates:
        if value is None or value == '':
            continue
        if not isinstance(value, str):
            _fail('invalid_email')
        value = value.strip().lower()
        if len(value) > 320 or value.count('@') != 1 or not all(value.split('@')) or any(
                ord(c) <= 32 or ord(c) >= 127 or c in '/\\\\?#%' for c in value):
            _fail('invalid_email')
        normalized.add(value)
    if len(normalized) > 1:
        _fail('ambiguous_email')
    return next(iter(normalized), None)


def _cooldowns(value):
    value = _json(value, {})
    if not isinstance(value, dict):
        _fail('invalid_cooldowns')
    result = {}
    for model, until in value.items():
        if not isinstance(model, str):
            _fail('invalid_cooldowns')
        lower = model.lower()
        group = ('argon' if 'argon' in lower else 'gemini' if 'gemini' in lower
                 else 'claude_gpt' if 'claude' in lower or 'gpt' in lower else model)
        result[group] = max(result.get(group, 0), _number(until))
    return result


def _credential(row: dict, data: dict, email: str, inactive: bool):
    value = {'email': email}
    for key in ('access_token', 'refresh_token', 'client_id', 'client_secret', 'project_id', 'expiry'):
        field = data.get(key) or (data.get('token') if key == 'access_token' else '') or ''
        if not isinstance(field, str):
            _fail('invalid_credential_field')
        value[key] = field
    if not value['refresh_token']:
        _fail('missing_refresh_token')
    value['disabled'] = int(inactive or _disabled(row.get('disabled', data.get('disabled', 0))))
    value['error_codes'] = _json(row.get('error_codes'), [])
    value['error_messages'] = _json(row.get('error_messages'), [])
    if not isinstance(value['error_codes'], list) or not isinstance(value['error_messages'], (list, dict)):
        _fail('invalid_error_state')
    value['model_cooldowns'] = _cooldowns(row.get('model_cooldowns'))
    value['last_success'] = _number(row.get('last_success'), None)
    tier = row.get('tier') or data.get('tier') or 'pro'
    if tier not in ('free', 'pro', 'ultra'):
        _fail('invalid_tier')
    value['tier'] = tier
    value['created_at'] = _number(row.get('created_at'))
    value['updated_at'] = _number(row.get('updated_at'), value['created_at'])
    return value


def _merge_credentials(a, b):
    # Same owner/email: keep newest OAuth fields, but never relax restrictive state.
    newest = dict(b if b['updated_at'] > a['updated_at'] else a)
    newest['disabled'] = max(a['disabled'], b['disabled'])
    newest['created_at'] = min(a['created_at'], b['created_at'])
    newest['model_cooldowns'] = dict(a['model_cooldowns'])
    for group, until in b['model_cooldowns'].items():
        newest['model_cooldowns'][group] = max(newest['model_cooldowns'].get(group, 0), until)
    newest['error_codes'] = list(a['error_codes'])
    for code in b['error_codes']:
        if code not in newest['error_codes']:
            newest['error_codes'].append(code)
    return newest


def _legacy_id(filename):
    return 'legacy-' + hashlib.sha256(filename.encode()).hexdigest()[:24] + '@migration.invalid'


def _load_legacy(db, owners, counts, email_map=None, preserve_collisions=False):
    tables = _tables(db)
    if 'antigravity_credentials' not in tables:
        _fail('unsupported_credentials_schema')
    if not {'filename', 'credential_data'} <= _columns(db, 'antigravity_credentials'):
        _fail('unsupported_credentials_schema')
    if 'credentials' in tables:
        counts['geminicli_archived'] += db.execute('SELECT COUNT(*) FROM credentials').fetchone()[0]
    groups, mappings, refresh_owners = {}, {}, {}
    records, email_counts, identity_map = [], {}, {}
    email_map = email_map or {}
    present_names = {r[0] for r in db.execute('SELECT filename FROM antigravity_credentials')}
    if set(email_map) - present_names:
        _fail('stale_email_map')
    for item in db.execute('SELECT * FROM antigravity_credentials ORDER BY filename'):
        row = dict(item)
        name = row['filename']
        if not isinstance(name, str) or not name or Path(name).name != name:
            _fail('unsafe_legacy_filename')
        data = _json(row['credential_data'])
        if not isinstance(data, dict):
            _fail('invalid_credential_json')
        counts['credentials_seen'] += 1
        email = _email(row, data)
        if name in email_map:
            mapped = email_map[name]
            if email and email != mapped:
                _fail('stale_email_map')
            if not email:
                email = mapped
                counts['mapped_emails'] += 1
        if email:
            email_counts[email] = email_counts.get(email, 0) + 1
        else:
            counts['synthetic_emails'] += 1
        mapping = owners.get(('antigravity', name))
        owner = mapping['user_id'] if mapping else ('__orphan__' if name.startswith('mu_') else '__admin__')
        state = mapping['state'] if mapping else 'active'
        refresh = data.get('refresh_token')
        if not isinstance(refresh, str) or not refresh:
            _fail('missing_refresh_token')
        token_identity = hashlib.sha256(refresh.encode()).hexdigest()
        if token_identity in refresh_owners and refresh_owners[token_identity] != owner:
            counts['ownership_conflicts'] += 1
            _fail('ownership_conflict', counts)
        refresh_owners[token_identity] = owner
        records.append((row, data, name, email, mapping, owner, state))
    # Resolve the complete collision set before assigning ANY destination ID.
    # Token ownership above is independent of aliases and has no opt-out.
    generated_ids = set()
    for row, data, name, original_email, mapping, owner, state in records:
        alias = bool(preserve_collisions and original_email and email_counts[original_email] > 1)
        generated = alias or not original_email
        email = _legacy_id(name) if generated else original_email
        if alias:
            counts['aliased_collision_credentials'] += 1
            identity_map[name] = {'original_email': original_email, 'database_id': email}
        # Never allow a generated ID to collide with a different record (including
        # a source address deliberately equal to that ID), even for one owner.
        if email in groups and (generated or email in generated_ids):
            _fail('migration_identity_collision', counts)
        if generated:
            generated_ids.add(email)
        group = groups.setdefault(email, {'owner': owner, 'credential': None, 'mappings': []})
        if group['owner'] != owner:
            counts['ownership_conflicts'] += 1
            _fail('ownership_conflict', counts)
        if owner == '__orphan__':
            counts['orphan_quarantined'] += 1
            continue
        inactive = state != 'active'
        if inactive:
            counts['inactive_credentials'] += 1
        credential = _credential(row, data, email, inactive)
        if group['credential']:
            counts['duplicate_credentials'] += 1
            credential = _merge_credentials(group['credential'], credential)
        group['credential'] = credential
        if mapping:
            mappings[name] = email
            group['mappings'].append(mapping)
    return groups, mappings, identity_map


def _migrate_accounts(path: Path, counts):
    with closing(_connect(path)) as db:
        required = {'users', 'api_keys', 'sessions', 'invites', 'credential_owners'}
        if not required <= _tables(db):
            _fail('unsupported_accounts_schema')
        if not {'mode', 'filename', 'user_id', 'created_at', 'state'} <= _columns(db, 'credential_owners'):
            _fail('unsupported_ownership_schema')
        for table in ('users', 'api_keys', 'sessions', 'invites'):
            counts[table] = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        users = {row[0] for row in db.execute('SELECT id FROM users')}
        owners = {}
        for row in db.execute('SELECT * FROM credential_owners'):
            value = dict(row)
            if value['state'] not in ('active', 'pending', 'deleted') or value['user_id'] not in users:
                _fail('invalid_ownership_record')
            owners[(value['mode'], value['filename'])] = value
        counts['ownership_rows'] = len(owners)
        if list(db.execute('PRAGMA foreign_key_check')):
            _fail('invalid_account_references')
        return owners


def _remap_owners(path: Path, owners, mappings, counts):
    # Delete+insert as one transaction avoids old filename/email collisions. Rows
    # with no matching credential remain as tombstones, never active ghosts.
    merged = {}
    priority = {'active': 0, 'pending': 1, 'deleted': 2}
    for key, row in owners.items():
        value = dict(row)
        if value['mode'] == 'antigravity' and value['filename'] in mappings:
            value['filename'] = mappings[value['filename']]
            counts['ownership_rows_remapped'] += 1
        elif value['mode'] == 'antigravity':
            if value['state'] == 'active':
                value['state'] = 'pending'
            counts['ownership_rows_unresolved'] += 1
        else:
            # Removed GeminiCLI API must not accidentally gain an active mapping.
            if value['state'] == 'active':
                value['state'] = 'pending'
        out_key = (value['mode'], value['filename'])
        previous = merged.get(out_key)
        if previous:
            if previous['user_id'] != value['user_id']:
                counts['ownership_conflicts'] += 1
                _fail('ownership_conflict', counts)
            value['state'] = max((previous['state'], value['state']), key=priority.get)
            value['created_at'] = min(previous['created_at'], value['created_at'])
        merged[out_key] = value
    with closing(_connect(path)) as db, db:
        db.execute('DELETE FROM credential_owners')
        for value in merged.values():
            db.execute('INSERT INTO credential_owners(mode,filename,user_id,created_at,state) VALUES(?,?,?,?,?)',
                       tuple(value[key] for key in ('mode', 'filename', 'user_id', 'created_at', 'state')))
        # Existing in-flight OAuth authorizations are unsafe across application
        # versions; preserve rows but consume pending requests rather than revive.
        if 'member_oauth_flows' in _tables(db):
            db.execute("UPDATE member_oauth_flows SET status='consumed' WHERE status='pending'")
        if list(db.execute('PRAGMA foreign_key_check')):
            _fail('invalid_account_references')


def _create_new_database(path, old_path, groups, counts):
    with closing(_connect(path)) as db, closing(_connect(old_path)) as legacy, db:
        db.executescript(NEW_SCHEMA)
        for group in groups.values():
            value = group['credential']
            if value is None:
                continue
            encoded = dict(value)
            for field in ('error_codes', 'error_messages', 'model_cooldowns'):
                encoded[field] = json.dumps(encoded[field], ensure_ascii=False)
            keys = list(encoded)
            db.execute(f"INSERT INTO antigravity_credentials({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                       [encoded[k] for k in keys])
            counts['credentials_migrated'] += 1
            counts['credentials_disabled'] += value['disabled']
        if 'config' in _tables(legacy):
            for row in legacy.execute('SELECT * FROM config'):
                row = dict(row)
                if row['key'] not in CONFIG_KEYS:
                    counts['configs_skipped'] += 1
                    continue
                # Preserve exact serialized value; new loader also accepts plain strings.
                db.execute('INSERT INTO config(key,value,updated_at) VALUES(?,?,?)',
                           (row['key'], row['value'], _number(row.get('updated_at'))))
                counts['configs_migrated'] += 1
    os.chmod(path, 0o600)


def _inventory(root):
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            _fail('target_changed')
        if path.is_file() and path.name != MANIFEST:
            files[str(path.relative_to(root))] = _hash(path)
    return files


def _existing_result(target, fingerprint, preserve_collisions=False):
    marker = target / MANIFEST
    if not marker.is_file() or marker.is_symlink():
        _fail('target_not_empty')
    try:
        manifest = json.loads(marker.read_text())
    except (ValueError, OSError):
        _fail('target_not_empty')
    if (manifest.get('version') != VERSION or manifest.get('source_fingerprint') != fingerprint
            or manifest.get('preserve_collisions') is not bool(preserve_collisions)
            or manifest.get('files') != _inventory(target)):
        _fail('target_not_empty')
    counts = manifest.get('counts')
    if not isinstance(counts, dict) or set(counts) != set(COUNTERS) or any(
            type(v) is not int or v < 0 for v in counts.values()):
        _fail('invalid_manifest')
    counts['already_migrated'] = 1
    return counts


def migrate(source, target, *, dry_run=False, accounts_db=None, credentials_db=None, email_map=None,
            preserve_collisions=False):
    """Return integer counters; raise MigrationError without leaking raw data."""
    counts = dict.fromkeys(COUNTERS, 0)
    try:
        source, target = Path(source).absolute(), Path(target).absolute()
        if source.is_symlink() or target.is_symlink():
            _fail('symlink_directory')
        source, target = source.resolve(), target.resolve()
        if source == target or source in target.parents or target in source.parents:
            _fail('overlapping_directories')
        if not source.is_dir() or not target.parent.is_dir():
            _fail('invalid_directory')
        accounts = Path(accounts_db).absolute() if accounts_db else source / 'multiuser.sqlite3'
        credentials = Path(credentials_db).absolute() if credentials_db else source / 'credentials.db'
        for db_path in (accounts, credentials):
            if target == db_path or target in db_path.parents:
                _fail('overlapping_directories')
            _regular(db_path)
        if accounts.resolve() == credentials.resolve():
            _fail('duplicate_database_path')
        with tempfile.TemporaryDirectory(prefix='.legacy-migrate-', dir=target.parent) as temporary:
            work = Path(temporary)
            stage, scratch = work / 'output', work / 'scratch'
            stage.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            archive = stage / 'legacy_archive'
            archive.mkdir(mode=0o700)
            observed = {}
            mapped_emails = {}
            if email_map is not None:
                map_path = Path(email_map).absolute()
                if map_path == target or target in map_path.parents:
                    _fail('invalid_email_map_path')
                local_map = scratch / 'email-map.json'
                observed[map_path] = _copy_readonly(map_path, local_map)
                raw_map = _email_map_json(local_map.read_bytes())
                if not isinstance(raw_map, dict):
                    _fail('invalid_email_map')
                for name, address in raw_map.items():
                    if not isinstance(name, str) or not name or Path(name).name != name:
                        _fail('invalid_email_map')
                    canonical = _email({'email': address}, {})
                    if canonical is None:
                        _fail('invalid_email_map')
                    mapped_emails[name] = canonical
            _backup(accounts, archive / 'multiuser.sqlite3', scratch, observed)
            _backup(credentials, archive / 'credentials.db', scratch, observed)
            # Legacy creds.toml contains per-file GeminiCLI credential sections,
            # NOT global configuration. Keep it only in the private archive.
            legacy_toml = source / 'creds.toml'
            if legacy_toml.exists():
                observed[legacy_toml] = _copy_readonly(legacy_toml, archive / 'creds.toml')
                counts['legacy_toml_archived'] = 1
            counts['sqlite_backups'] = 2
            _verify_source(observed)
            fingerprint = _fingerprint(observed)
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                return _existing_result(target, fingerprint, preserve_collisions)
            shutil.copyfile(archive / 'multiuser.sqlite3', stage / 'multiuser.sqlite3')
            os.chmod(stage / 'multiuser.sqlite3', 0o600)
            owners = _migrate_accounts(stage / 'multiuser.sqlite3', counts)
            with closing(_connect(archive / 'credentials.db')) as legacy:
                groups, mappings, identity_map = _load_legacy(
                    legacy, owners, counts, mapped_emails, preserve_collisions)
            if identity_map:
                identity_path = archive / 'identity-map.json'
                identity_path.write_text(json.dumps(identity_map, sort_keys=True) + '\n')
                os.chmod(identity_path, 0o600)
            _remap_owners(stage / 'multiuser.sqlite3', owners, mappings, counts)
            _create_new_database(stage / 'credentials.db', archive / 'credentials.db', groups, counts)
            # Recheck source AFTER all work, including WAL absence/presence.
            _verify_source(observed)
            manifest = {'version': VERSION, 'source_fingerprint': fingerprint,
                        'preserve_collisions': bool(preserve_collisions),
                        'counts': counts, 'files': _inventory(stage)}
            marker = stage / MANIFEST
            marker.write_text(json.dumps(manifest, sort_keys=True) + '\n')
            os.chmod(marker, 0o600)
            if not dry_run:
                if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
                    _fail('target_not_empty')
                # POSIX rename cannot replace a nonempty directory, even if one
                # appears between this check and the rename.
                os.rename(stage, target)
            return counts
    except MigrationError as error:
        for key, value in counts.items():
            error.counts.setdefault(key, value)
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        raise MigrationError('migration_failed', counts) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', required=True, help='Stopped legacy creds directory (never modified)')
    parser.add_argument('--target', required=True, help='Absent/empty new creds directory; parent must exist')
    parser.add_argument('--accounts-db', help='Override legacy multiuser.sqlite3 path')
    parser.add_argument('--credentials-db', help='Override legacy credentials.db path')
    parser.add_argument('--email-map', help='Trusted private JSON object mapping legacy filename to email; fills missing identities only')
    parser.add_argument('--preserve-collisions', action='store_true',
                        help='Keep each filename in duplicate-email groups under its own stable ID; NEVER bypass cross-owner refresh-token conflicts')
    parser.add_argument('--dry-run', action='store_true', help='Validate full migration without publishing target')
    parser.add_argument('--report', help='Optional new JSON report file outside source and target (never overwritten)')
    args = parser.parse_args(argv)
    exit_code, report_fd = 0, None
    try:
        if args.report:
            report = Path(args.report).absolute()
            resolved = report.resolve()
            for root in (Path(args.source).resolve(), Path(args.target).resolve()):
                if resolved == root or root in resolved.parents:
                    _fail('invalid_report_path')
            # Reserve a NEW report before any publication; missing/unwritable
            # report parents must not cause a half-successful CLI invocation.
            report_fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        counts = migrate(args.source, args.target, dry_run=args.dry_run,
                         accounts_db=args.accounts_db, credentials_db=args.credentials_db,
                         email_map=args.email_map, preserve_collisions=args.preserve_collisions)
    except MigrationError as error:
        counts = dict.fromkeys(COUNTERS, 0)
        counts.update(error.counts)
        exit_code = 1
    except OSError:
        counts = dict.fromkeys(COUNTERS, 0)
        counts['errors'] = 1
        exit_code = 1
    text = json.dumps(counts, sort_keys=True) + '\n'
    sys.stdout.write(text)
    if report_fd is not None:
        try:
            with os.fdopen(report_fd, 'w') as stream:
                stream.write(text)
        except OSError:
            return 1
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
