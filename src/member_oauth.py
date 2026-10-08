"""Member-only Antigravity OAuth; include this router separately in the app.

Manual loopback callback: no listener, original redirect, no global pool helpers.
SQLite holds only flow metadata; codes/tokens exist only for the active request.
A claimed flow is one-use even if an upstream or storage operation fails.
"""

import asyncio
from contextvars import ContextVar
from datetime import timezone
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, Request

from config import get_antigravity_api_url
from . import google_oauth_api
from .google_oauth_api import Flow, fetch_project_id_and_tier
from .multiuser import (_error, get_multiuser_store, parse_credential,
                        require_member, store_member_credential)
from .utils import (ANTIGRAVITY_CLIENT_ID, ANTIGRAVITY_CLIENT_SECRET,
                    ANTIGRAVITY_SCOPES, ANTIGRAVITY_USER_AGENT, CALLBACK_HOST,
                    TOKEN_URL)

REDIRECT_URI = f'http://{CALLBACK_HOST}:11451'
FLOW_TTL = 15 * 60
router = APIRouter(prefix='/users/oauth', tags=['member-oauth'])

# Original Flow/project helpers log upstream exception/response bodies. Suppress
# those logs ONLY in this request context, not globally during await (which would
# affect unrelated requests). No changes to legacy panel behavior or HTTP calls.
_private_oauth = ContextVar('member_oauth_private', default=False)


class _PrivateOAuthLogger:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        method = getattr(self.original, name)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            if not _private_oauth.get():
                return method(*args, **kwargs)
        return call


google_oauth_api.log = _PrivateOAuthLogger(google_oauth_api.log)


def _flow(redirect_uri: str):
    # OAuth application configuration is supplied by the deployment, not source.
    if not ANTIGRAVITY_CLIENT_ID or not ANTIGRAVITY_CLIENT_SECRET:
        raise ValueError('Antigravity OAuth client is not configured')
    # Original Flow has no PKCE implementation; do not fabricate one.
    return Flow(client_id=ANTIGRAVITY_CLIENT_ID,
                client_secret=ANTIGRAVITY_CLIENT_SECRET,
                scopes=ANTIGRAVITY_SCOPES, redirect_uri=redirect_uri)


async def _body(request: Request, fields: set) -> dict:
    # Avoid Pydantic 422 input echoes containing an auth code or callback URL.
    # The proxy/application should also enforce its usual HTTP body size limit.
    try:
        raw = await request.body()
        if len(raw) > 16384:
            _error(400, 'Invalid OAuth request')
        data = {} if not raw and not fields else await request.json()
    except Exception:
        _error(400, 'Invalid OAuth request')
    if not isinstance(data, dict) or set(data) != fields:
        _error(400, 'Invalid OAuth request')
    return data


def _callback(value):
    """Validate exact loopback origin/path, then extract unique code and state.

    Only the fixed server redirect is accepted; the supplied URL is never fetched.
    Empty path and '/' denote the same loopback root as in browser callbacks.
    """
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
                or re.search(r'%(?![0-9A-Fa-f]{2})', value)):
            raise ValueError()
        parsed, expected = urlsplit(value), urlsplit(REDIRECT_URI)
        if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc
                or parsed.hostname != expected.hostname or parsed.port != expected.port
                or parsed.path not in ('', '/') or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            raise ValueError()
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=20)
        params = {}
        for key, item in pairs:
            if key in params or key in ('error', 'error_description', 'error_uri'):
                raise ValueError()
            params[key] = item
        code, state = params.get('code'), params.get('state')
        if (not isinstance(code, str) or not 1 <= len(code) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in code)
                or not isinstance(state, str)
                or re.fullmatch(r'[A-Za-z0-9_-]{43}', state) is None):
            raise ValueError()
        return code, state
    except (ValueError, UnicodeError):
        _error(400, 'Invalid OAuth callback')


@router.post('/start')
async def start(request: Request, user: dict = Depends(require_member)):
    await _body(request, set())
    metadata = await asyncio.to_thread(get_multiuser_store().create_oauth_flow,
                                      user['id'], REDIRECT_URI, FLOW_TTL)
    try:
        auth_url = _flow(metadata['redirect_uri']).get_auth_url(state=metadata['state'])
    except Exception:
        _error(502, 'OAuth initialization failed; start again')
    return {'auth_url': auth_url, **metadata}


@router.post('/complete')
async def complete(request: Request, user: dict = Depends(require_member)):
    body = await _body(request, {'callback_url'})
    code, state = _callback(body['callback_url'])
    # owner + pending + unexpired + exact server redirect in a single transaction.
    # Never trust client fields to choose owner/project/redirect.
    await asyncio.to_thread(get_multiuser_store().claim_oauth_flow,
                            user['id'], state, REDIRECT_URI)
    privacy = _private_oauth.set(True)
    try:
        try:
            credentials = await _flow(REDIRECT_URI).exchange_code(code)
            project_id, tier = await fetch_project_id_and_tier(
                credentials.access_token, ANTIGRAVITY_USER_AGENT,
                await get_antigravity_api_url())
            if not project_id:
                raise ValueError()
            data = {
                'client_id': ANTIGRAVITY_CLIENT_ID,
                'client_secret': ANTIGRAVITY_CLIENT_SECRET,
                'token': credentials.access_token,
                'refresh_token': credentials.refresh_token,
                'scopes': ANTIGRAVITY_SCOPES,
                'token_uri': TOKEN_URL,
                'project_id': project_id,
            }
            if credentials.expires_at:
                expiry = credentials.expires_at
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                data['expiry'] = expiry.astimezone(timezone.utc).isoformat()
            # Validate project/tokens exactly as member uploads do, but never
            # return validation/upstream details or any credential contents.
            data = parse_credential(json.dumps(data).encode('utf-8'))
        except Exception:
            _error(502, 'OAuth authorization failed; authorize again')
        credential = await store_member_credential(user['id'], data, 'antigravity', {
            'error_codes': [], 'disabled': False, 'last_success': time.time(),
            'user_email': None, 'tier': tier,
        })
        return {'credential': credential, 'success': True}
    finally:
        _private_oauth.reset(privacy)
