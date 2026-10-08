"""Invite-only member accounts, isolated credentials and opaque access tokens.

Integration (this module deliberately does not modify the existing application):
* app.include_router(router).
* Await authenticate_user_api(raw_token) INLINE in the existing API auth verifier,
  not in a synchronous dependency/thread, so current_owner reaches the selector.
  Reset current_owner at request entry/exit (including streaming lifetimes). The
  authenticator clears it before validation, and never falls back on invalid keys.
* Filter EVERY selection/retry/quota path with owned_filenames(owner, mode, names).
  Both ownership helpers are synchronous; current_owner defaults to None. Treat
  None as '__admin__' only in legacy/internal selection, never as authentication.
* Existing panel authentication remains admin-only; member tokens are never sent
  to verify_panel_token. Invite creation uses the existing panel password as Bearer.

Storage: MULTIUSER_DB_PATH, default $CREDENTIALS_DIR/multiuser.sqlite3 (./creds).
This DB contains password scrypt hashes, SHA256 token hashes and ownership, not
Google credentials. Ownership is reserved BEFORE adapter writes. Failed/deleted
uploads retain their mapping (tombstone), so orphan/cached files never become
implicit admin credentials. Unmapped legacy files belong to '__admin__'; unmapped
mu_ member files fail closed as '__orphan__', including after database loss.

HTTP API (all times are integer Unix seconds; IDs are opaque strings):
 POST /users/invites    admin Bearer; {expires_in?:86400,expires_hours?:72}
                       -> {invite,expires_at}; expires_hours takes precedence
 POST /users/register   {invite,username,password} -> {user,token,session_token,expires_at}
 POST /users/login      {username,password} -> {user,token,session_token,expires_at}
 GET  /users/me         member session Bearer -> {user,id,username,created_at}
 POST /users/logout     member session Bearer -> {success:true}
 GET  /users/keys       member session -> {keys:[Key]}
 POST /users/keys       {name?:'default',expires_in?:2592000} -> {key,api_key,key_info:Key}
                       key and api_key are aliases for the same secret string
 DELETE /users/keys/{id} member session -> {success:true}
 GET  /users/credentials?mode=antigravity -> {credentials:[Credential]}
 POST /users/credentials (alias /credentials/upload): multipart file OR repeated
                       files, mode form/query -> {credential:Credential,credentials:[Credential]}
                       1-10 JSON files, each <=256 KiB; query mode takes precedence
 DELETE /users/credentials/{filename}?mode=antigravity -> {success:true}
 User = {id,username,created_at}; Key = {id,name,created_at,expires_at,revoked_at};
 Credential = {filename,mode,created_at}. No list route returns tokens/credentials.
 Default mode is antigravity; geminicli is also supported. Session lifetime is
 seven days; invitation/key lifetimes are bounded. Tokens are returned once only.
 Errors use FastAPI {detail:...}; invalid auth 401, wrong ownership 404, validation
 400/422, oversized uploads 413, adapter failure 503. No public registration.
"""

import asyncio
from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import lru_cache
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from config import get_api_password
from .storage_adapter import get_storage_adapter
from .utils import verify_panel_token

ADMIN_OWNER = '__admin__'
ORPHAN_OWNER = '__orphan__'
MEMBER_FILENAME_PREFIX = 'mu_'
current_owner: ContextVar[Optional[str]] = ContextVar('multiuser_current_owner', default=None)
SESSION_TTL = 7 * 24 * 3600
MAX_UPLOAD_BYTES = 256 * 1024
INVITE_PREFIX = 'mui_'
SESSION_PREFIX = 'mus_'
KEY_PREFIX = 'muk_'
MODES = ('antigravity', 'geminicli')
_USERNAME = re.compile(r'[a-z0-9][a-z0-9_.-]{2,63}\Z')
_PROJECT = re.compile(r'(?:[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]{6,30})\Z')
_TOKEN_URLS = {'https://oauth2.googleapis.com/token', 'https://www.googleapis.com/oauth2/v4/token'}


def _error(code: int, message: str):
    raise HTTPException(status_code=code, detail=message)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _valid_token(token: str, prefix: str) -> bool:
    return isinstance(token, str) and re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_-]{43}', token) is not None


def _username(value: str) -> str:
    value = value.strip().lower()
    if not _USERNAME.fullmatch(value):
        _error(400, 'Username must be 3-64 ASCII letters, digits, dots, dashes or underscores')
    return value


def _password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                          n=16384, r=8, p=1, dklen=32).hex()


def _validate_password(password: str):
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        _error(400, 'Password must contain 10-256 characters')


def _mode(mode: str) -> str:
    if mode not in MODES:
        _error(400, 'Unsupported credential mode')
    return mode


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
 password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
 created_at INTEGER NOT NULL, disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invites (
 token_hash TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
 expires_at INTEGER NOT NULL, used_at INTEGER, user_id TEXT REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
 user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS api_keys_owner ON api_keys(user_id);
CREATE TABLE IF NOT EXISTS credential_owners (
 mode TEXT NOT NULL, filename TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
 created_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 PRIMARY KEY(mode, filename)
);
CREATE INDEX IF NOT EXISTS credential_owner_user ON credential_owners(user_id, mode);
CREATE TABLE IF NOT EXISTS member_oauth_flows (
 state TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 redirect_uri TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS member_oauth_owner ON member_oauth_flows(user_id, status);
'''


class MultiuserStore:
    """Small synchronous SQLite repository. Async routes run it in a worker thread.

    Registration uses BEGIN IMMEDIATE: invite validation, account creation, invite
    consumption and initial session are a single transaction, including rollback
    for duplicate usernames. Connections are short-lived and never shared.
    """

    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(_SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def public_user(row):
        return {key: row[key] for key in ('id', 'username', 'created_at')}

    def create_invite(self, expires_in: int = 86400):
        if not 60 <= expires_in <= 30 * 86400:
            _error(400, 'Invite lifetime must be 60-2592000 seconds')
        token, now = _new_token(INVITE_PREFIX), int(time.time())
        with self.transaction() as db:
            db.execute('INSERT INTO invites(token_hash,created_at,expires_at) VALUES(?,?,?)',
                       (_digest(token), now, now + expires_in))
        return {'invite': token, 'expires_at': now + expires_in}

    def _session(self, db, user, now):
        token = _new_token(SESSION_PREFIX)
        db.execute('INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)',
                   (_digest(token), user['id'], now, now + SESSION_TTL))
        return {'user': self.public_user(user), 'token': token, 'session_token': token, 'expires_at': now + SESSION_TTL}

    def register(self, invite: str, username: str, password: str):
        username = _username(username)
        _validate_password(password)
        if not _valid_token(invite, INVITE_PREFIX):
            _error(400, 'Invalid or expired invitation')
        salt = secrets.token_hex(16)
        password_hash = _password_hash(password, salt)
        with self.transaction() as db:
            now = int(time.time())
            row = db.execute('SELECT * FROM invites WHERE token_hash=?', (_digest(invite),)).fetchone()
            if not row or row['used_at'] is not None or row['expires_at'] <= now:
                _error(400, 'Invalid or expired invitation')
            user = {'id': uuid.uuid4().hex, 'username': username, 'created_at': now}
            try:
                db.execute('INSERT INTO users(id,username,password_salt,password_hash,created_at) VALUES(?,?,?,?,?)',
                           (user['id'], username, salt, password_hash, now))
            except sqlite3.IntegrityError:
                _error(409, 'Username already exists')
            db.execute('UPDATE invites SET used_at=?,user_id=? WHERE token_hash=?',
                       (now, user['id'], _digest(invite)))
            return self._session(db, user, now)

    def login(self, username: str, password: str):
        # Generic response; unknown accounts still pay a scrypt computation.
        username = username.strip().lower()
        if not isinstance(password, str) or len(password) > 256:
            _error(401, 'Invalid username or password')
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        computed = _password_hash(password, row['password_salt'] if row else '00' * 16)
        if not row or not hmac.compare_digest(computed, row['password_hash']) or row['disabled']:
            _error(401, 'Invalid username or password')
        with self.transaction() as db:
            # Recheck in the transaction in case an administrator disabled the user.
            user = db.execute('SELECT * FROM users WHERE id=? AND disabled=0', (row['id'],)).fetchone()
            if not user:
                _error(401, 'Invalid username or password')
            return self._session(db, user, int(time.time()))

    def resolve_session(self, token: str):
        if not _valid_token(token, SESSION_PREFIX):
            _error(401, 'Invalid member session')
        with closing(self._connect()) as db:
            row = db.execute('''SELECT u.* FROM sessions s JOIN users u ON s.user_id=u.id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?
                AND u.disabled=0''', (_digest(token), int(time.time()))).fetchone()
        if not row:
            _error(401, 'Invalid or expired member session')
        return self.public_user(row)

    def logout(self, token: str):
        with self.transaction() as db:
            db.execute('UPDATE sessions SET revoked_at=? WHERE token_hash=?',
                       (int(time.time()), _digest(token)))

    def create_key(self, user_id: str, name: str = 'default', expires_in: int = 30 * 86400):
        name = name.strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            _error(400, 'Key name must contain 1-80 printable characters')
        if not 60 <= expires_in <= 365 * 86400:
            _error(400, 'Key lifetime must be 60-31536000 seconds')
        now, token = int(time.time()), _new_token(KEY_PREFIX)
        key = {'id': uuid.uuid4().hex, 'name': name, 'created_at': now,
               'expires_at': now + expires_in, 'revoked_at': None}
        with self.transaction() as db:
            db.execute('''INSERT INTO api_keys(id,token_hash,user_id,name,created_at,expires_at)
                          VALUES(?,?,?,?,?,?)''',
                       (key['id'], _digest(token), user_id, name, now, key['expires_at']))
        return {'key': token, 'api_key': token, 'key_info': key}

    def list_keys(self, user_id: str):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute('''SELECT id,name,created_at,expires_at,revoked_at
                FROM api_keys WHERE user_id=? ORDER BY created_at,id''', (user_id,))]

    def revoke_key(self, user_id: str, key_id: str):
        with self.transaction() as db:
            result = db.execute('''UPDATE api_keys SET revoked_at=COALESCE(revoked_at,?)
                                  WHERE id=? AND user_id=?''', (int(time.time()), key_id, user_id))
            if not result.rowcount:
                _error(404, 'Key not found')

    def resolve_api_key(self, token: str):
        if not _valid_token(token, KEY_PREFIX):
            return None
        with closing(self._connect()) as db:
            row = db.execute('''SELECT k.user_id FROM api_keys k JOIN users u ON u.id=k.user_id
                WHERE k.token_hash=? AND k.revoked_at IS NULL AND k.expires_at>?
                AND u.disabled=0''', (_digest(token), int(time.time()))).fetchone()
        return row['user_id'] if row else None

    def create_oauth_flow(self, user_id: str, redirect_uri: str, ttl: int = 900):
        """Persist metadata only; bound pending flows and clean expired rows globally."""
        now, state = int(time.time()), secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute('DELETE FROM member_oauth_flows WHERE expires_at<=?', (now,))
            # A fresh start supersedes that member's previous pending link.
            # Consumed rows remain until expiry; replay is rejected in all cases.
            db.execute("DELETE FROM member_oauth_flows WHERE user_id=? AND status='pending'", (user_id,))
            total = db.execute('SELECT COUNT(*) FROM member_oauth_flows').fetchone()[0]
            if total >= 200:
                _error(429, 'Too many OAuth flows; retry later')
            db.execute('''INSERT INTO member_oauth_flows(state,user_id,redirect_uri,created_at,expires_at)
                          VALUES(?,?,?,?,?)''', (state, user_id, redirect_uri, now, now + ttl))
        return {'state': state, 'redirect_uri': redirect_uri, 'expires_at': now + ttl}

    def claim_oauth_flow(self, user_id: str, state: str, redirect_uri: str):
        """Exactly one owner may claim a live flow, before any network operation.

        Wrong owners cannot consume it. Claims remain consumed after failures,
        cancellation or crashes: an authorization code is never retried.
        """
        with self.transaction() as db:
            result = db.execute('''UPDATE member_oauth_flows SET status='consumed'
                WHERE state=? AND user_id=? AND redirect_uri=? AND expires_at>?
                AND status='pending' ''', (state, user_id, redirect_uri, int(time.time())))
            if result.rowcount != 1:
                _error(400, 'Invalid or expired OAuth flow; start again')

    def reserve_credential(self, user_id: str, filename: str, mode: str):
        with self.transaction() as db:
            db.execute('''INSERT INTO credential_owners(mode,filename,user_id,created_at,state)
                          VALUES(?,?,?,?,'pending')''', (_mode(mode), filename, user_id, int(time.time())))

    def credential_state(self, user_id: str, filename: str, mode: str, state: str):
        if state not in ('pending', 'active', 'deleted'):
            raise ValueError('Invalid ownership state')
        with self.transaction() as db:
            result = db.execute('''UPDATE credential_owners SET state=?
                WHERE filename=? AND mode=? AND user_id=?''', (state, filename, _mode(mode), user_id))
            if not result.rowcount:
                _error(404, 'Credential not found')

    def credential_owner(self, filename: str, mode: str):
        with closing(self._connect()) as db:
            row = db.execute('SELECT user_id FROM credential_owners WHERE filename=? AND mode=?',
                             (filename, _mode(mode))).fetchone()
        return row['user_id'] if row else (ORPHAN_OWNER if filename.startswith(MEMBER_FILENAME_PREFIX) else ADMIN_OWNER)

    def list_credentials(self, user_id: str, mode: str):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute('''SELECT filename,mode,created_at
                FROM credential_owners WHERE user_id=? AND mode=? AND state='active'
                ORDER BY created_at,filename''', (user_id, _mode(mode)))]

    def owned_filenames(self, owner: str, mode: str, all_filenames=None):
        with closing(self._connect()) as db:
            rows = db.execute('SELECT filename,user_id,state FROM credential_owners WHERE mode=?',
                              (_mode(mode),)).fetchall()
        mapped = {row['filename'] for row in rows}
        owned = {row['filename'] for row in rows if row['user_id'] == owner and row['state'] == 'active'}
        if owner == ADMIN_OWNER:
            if all_filenames is None:
                raise ValueError('Admin filtering requires the storage filename list')
            return [name for name in all_filenames if name not in mapped and not name.startswith(MEMBER_FILENAME_PREFIX)]
        if all_filenames is None:
            return sorted(owned)
        return [name for name in all_filenames if name in owned]


@lru_cache(maxsize=8)
def _store_for_path(path: str):
    return MultiuserStore(path)


def get_multiuser_store() -> MultiuserStore:
    """Lazy initialization; an environment override can also be used by tests."""
    path = os.getenv('MULTIUSER_DB_PATH') or str(Path(os.getenv('CREDENTIALS_DIR', './creds')) / 'multiuser.sqlite3')
    return _store_for_path(str(Path(path).expanduser().resolve()))


def credential_owner(filename: str, mode: str = 'antigravity') -> str:
    """Synchronous; mapped tombstones retain ownership, unowned mu_ files fail closed.

    Use owned_filenames for selection: this lookup intentionally reports ownership
    even for pending/deleted rows; it is not an active-credential eligibility check.
    """
    return get_multiuser_store().credential_owner(filename, mode)


def owned_filenames(owner: str, mode: str, all_filenames=None):
    """Synchronous selector filter; preserve caller ordering, fail closed for None."""
    return get_multiuser_store().owned_filenames(owner, mode, all_filenames)


async def authenticate_user_api(token: str, allow_legacy: bool = True) -> str:
    """Return/set owner or raise 401. Only member API keys or exact legacy password.

    Legacy acceptance can be disabled with allow_legacy=False or environment
    MULTIUSER_ALLOW_LEGACY_API_PASSWORD=0/false/no. Sessions/invites/invalid member
    keys never fall through to legacy authentication, even on a password collision.
    """
    current_owner.set(None)
    if not isinstance(token, str) or not token or len(token) > 1024:
        _error(401, 'Invalid API key')
    if token.startswith(KEY_PREFIX):
        owner = await asyncio.to_thread(get_multiuser_store().resolve_api_key, token)
        if owner is None:
            _error(401, 'Invalid or expired API key')
    elif token.startswith((SESSION_PREFIX, INVITE_PREFIX)):
        _error(401, 'Member sessions and invites are not API keys')
    else:
        enabled = os.getenv('MULTIUSER_ALLOW_LEGACY_API_PASSWORD', 'true').lower() not in ('0', 'false', 'no')
        password = await get_api_password() if allow_legacy and enabled else None
        if not isinstance(password, str) or not password or not hmac.compare_digest(token.encode(), password.encode()):
            _error(401, 'Invalid API key')
        owner = ADMIN_OWNER
    current_owner.set(owner)
    return owner


class InviteRequest(BaseModel):
    expires_in: int = Field(default=86400, ge=60, le=30 * 86400)
    expires_hours: Optional[int] = Field(default=None, ge=1, le=720)


class RegisterRequest(BaseModel):
    invite: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class KeyRequest(BaseModel):
    name: str = Field(default='default', min_length=1, max_length=80)
    expires_in: int = Field(default=30 * 86400, ge=60, le=365 * 86400)


router = APIRouter(prefix='/users', tags=['users'])
_member_bearer = HTTPBearer(auto_error=False)


async def require_member(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_member_bearer)):
    if not credentials:
        _error(401, 'Member session required')
    return await asyncio.to_thread(get_multiuser_store().resolve_session, credentials.credentials)


@router.post('/invites')
async def create_invite(body: InviteRequest, _admin: str = Depends(verify_panel_token)):
    lifetime = body.expires_hours * 3600 if body.expires_hours is not None else body.expires_in
    return await asyncio.to_thread(get_multiuser_store().create_invite, lifetime)


@router.post('/register')
async def register(body: RegisterRequest):
    return await asyncio.to_thread(get_multiuser_store().register, body.invite, body.username, body.password)


@router.post('/login')
async def login(body: LoginRequest):
    return await asyncio.to_thread(get_multiuser_store().login, body.username, body.password)


@router.get('/me')
async def me(user: dict = Depends(require_member)):
    return {'user': user, **user}


@router.post('/logout')
async def logout(user: dict = Depends(require_member), credentials: HTTPAuthorizationCredentials = Depends(_member_bearer)):
    await asyncio.to_thread(get_multiuser_store().logout, credentials.credentials)
    return {'success': True}


@router.get('/keys')
async def list_keys(user: dict = Depends(require_member)):
    return {'keys': await asyncio.to_thread(get_multiuser_store().list_keys, user['id'])}


@router.post('/keys')
async def create_key(body: KeyRequest, user: dict = Depends(require_member)):
    return await asyncio.to_thread(get_multiuser_store().create_key, user['id'], body.name, body.expires_in)


@router.delete('/keys/{key_id}')
async def revoke_key(key_id: str, user: dict = Depends(require_member)):
    await asyncio.to_thread(get_multiuser_store().revoke_key, user['id'], key_id)
    return {'success': True}


def parse_credential(content: bytes) -> dict:
    """Validate JSON and whitelist Google OAuth fields; never trust token endpoints."""
    if len(content) > MAX_UPLOAD_BYTES:
        _error(413, 'Credential exceeds 256 KiB')
    try:
        data = json.loads(content.decode('utf-8'))
    except (UnicodeError, ValueError, RecursionError):
        _error(400, 'Credential must be a UTF-8 JSON object')
    if not isinstance(data, dict):
        _error(400, 'Credential must be a JSON object')
    refresh = data.get('refresh_token')
    project = data.get('project_id')
    if not isinstance(refresh, str) or not 1 <= len(refresh) <= 16384 or refresh != refresh.strip() or any(ord(c) < 33 for c in refresh):
        _error(400, 'A valid refresh_token is required')
    if not isinstance(project, str) or not _PROJECT.fullmatch(project):
        _error(400, 'A valid Google project_id or project number is required')
    allowed = ('refresh_token', 'project_id', 'client_id', 'client_secret', 'token', 'access_token', 'expiry', 'token_uri')
    result = {key: data[key] for key in allowed if key in data}
    for key, value in result.items():
        if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 32 for c in value):
            _error(400, 'Invalid OAuth credential field')
    if 'token_uri' in result and result['token_uri'] not in _TOKEN_URLS:
        _error(400, 'Unsupported OAuth token endpoint')
    if 'scopes' in data:
        scopes = data['scopes']
        if not isinstance(scopes, list) or len(scopes) > 30 or any(not isinstance(s, str) or len(s) > 1024 for s in scopes):
            _error(400, 'Invalid OAuth scopes')
        result['scopes'] = scopes
    return result


@router.get('/credentials')
async def list_credentials(mode: str = 'antigravity', user: dict = Depends(require_member)):
    _mode(mode)
    rows = await asyncio.to_thread(get_multiuser_store().list_credentials, user['id'], mode)
    # Hide records whose adapter write disappeared; never expose credential contents.
    adapter = await get_storage_adapter()
    present = set(await adapter.list_credentials(mode=mode))
    return {'credentials': [row for row in rows if row['filename'] in present]}


async def store_member_credential(user_id: str, data: dict, mode: str,
                                  state_updates: Optional[dict] = None) -> dict:
    """Reserve ownership before writing; only activate after all writes succeed.

    A partial write retains pending ownership and is ineligible for every owner,
    including the administrator. OAuth can initialize adapter quota/tier state.
    """
    _mode(mode)
    store = get_multiuser_store()
    filename = MEMBER_FILENAME_PREFIX + uuid.uuid4().hex + '.json'
    try:
        # Synchronous tiny reservation prevents cancellation before ownership exists.
        store.reserve_credential(user_id, filename, mode)
        adapter = await get_storage_adapter()
        if not await adapter.store_credential(filename, data, mode=mode):
            _error(503, 'Credential storage failed')
        if state_updates is not None:
            result = await adapter.update_credential_state(filename, state_updates, mode=mode)
            if result is False:
                _error(503, 'Credential storage failed')
        store.credential_state(user_id, filename, mode, 'active')
        return next(row for row in store.list_credentials(user_id, mode) if row['filename'] == filename)
    except Exception:
        _error(503, 'Credential storage failed')


@router.post('/credentials/upload', include_in_schema=False)
@router.post('/credentials')
async def upload_credential(request: Request, file: Optional[UploadFile] = File(None),
                            files: Optional[list[UploadFile]] = File(None),
                            mode: str = 'antigravity', user: dict = Depends(require_member)):
    # The UI posts repeated 'files' and form mode. Also support a single 'file'
    # and query mode for API clients; explicit query mode takes precedence.
    if 'mode' not in request.query_params:
        form = await request.form()
        mode = form.get('mode', mode)
    _mode(mode)
    uploads = ([file] if file is not None else []) + (files or [])
    if not 1 <= len(uploads) <= 10:
        _error(400, 'Upload 1-10 JSON credential files')
    # Validate the entire batch before persisting anything. Adapter failures may
    # still leave earlier successful files; those remain visible to their owner.
    parsed = [parse_credential(await item.read(MAX_UPLOAD_BYTES + 1)) for item in uploads]
    created = []
    for data in parsed:
        created.append(await store_member_credential(user['id'], data, mode))
    return {'credential': created[0], 'credentials': created}


@router.delete('/credentials/{filename}')
async def delete_credential(filename: str, mode: str = 'antigravity', user: dict = Depends(require_member)):
    _mode(mode)
    store = get_multiuser_store()
    if store.credential_owner(filename, mode) != user['id']:
        _error(404, 'Credential not found')
    # Hide before deletion, so selectors cannot pick a file being removed. A
    # failed deletion keeps a tombstone; members can retry this same endpoint.
    store.credential_state(user['id'], filename, mode, 'deleted')
    adapter = await get_storage_adapter()
    try:
        if not await adapter.delete_credential(filename, mode=mode):
            # Deleting an already absent file is idempotent.
            if await adapter.get_credential(filename, mode=mode) is not None:
                _error(503, 'Credential deletion failed')
    except Exception:
        _error(503, 'Credential deletion failed')
    return {'success': True}
