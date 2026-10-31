"""Compatibility seam: legacy personal APIs backed by email-keyed SQL storage.

Never derive identity from a user-supplied email or filename. Refresh the supplied
refresh token first, and obtain its email from Google's userinfo endpoint.
"""
import asyncio
from contextvars import ContextVar
import hashlib
import hmac

from .. import google_oauth_api
from ..google_oauth_api import Credentials

private_operation = ContextVar('multiuser_private_operation', default=False)
credential_ingest_lock = asyncio.Lock()


async def assert_unique_refresh_token(database, email, refresh_tokens):
    """Reject synthetic/real-email duplicates without logging token material."""
    from .accounts import _error
    fingerprints = [hashlib.sha256(token.encode()).digest()
                    for token in refresh_tokens if isinstance(token, str) and token]
    for existing_email in await database.list_credentials():
        if existing_email == email:
            continue
        existing = await database.get_credential(existing_email)
        token = (existing or {}).get('refresh_token')
        if not isinstance(token, str) or not token:
            continue
        digest = hashlib.sha256(token.encode()).digest()
        if any(hmac.compare_digest(digest, candidate) for candidate in fingerprints):
            _error(409, 'Credential already exists under another identity')


class PrivateLogger:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        method = getattr(self.original, name)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            if not private_operation.get():
                return method(*args, **kwargs)
        return call


google_oauth_api.log = PrivateLogger(google_oauth_api.log)


def canonical_email(value):
    if not isinstance(value, str):
        raise ValueError('Missing credential email')
    email = value.strip().lower()
    if (len(email) > 320 or email.count('@') != 1 or not all(email.split('@'))
            or any(ord(c) <= 32 or ord(c) >= 127 or c in '/\\?#%' for c in email)):
        raise ValueError('Invalid credential email')
    return email


async def resolve_credential_identity(data):
    from .accounts import _error
    privacy = private_operation.set(True)
    try:
        # The access token and email in an upload need not correspond to its
        # refresh token. Verify ownership using a fresh access token instead.
        credentials = Credentials.from_dict(data)
        await credentials.refresh()
        email = canonical_email(await google_oauth_api.get_user_email(credentials))
        for field in ('email', 'user_email'):
            if data.get(field) and canonical_email(data[field]) != email:
                _error(400, 'Credential identity does not match')
        return email, {**data, **credentials.to_dict(), 'token': credentials.access_token}
    except Exception as error:
        from fastapi import HTTPException
        if isinstance(error, HTTPException):
            raise
        _error(502, 'Credential identity verification failed')
    finally:
        private_operation.reset(privacy)


class StorageAdapter:
    """Antigravity-only legacy call signatures, no duplicate credential storage."""
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _mode(mode):
        from .accounts import _mode
        _mode(mode)

    async def list_credentials(self, mode='antigravity'):
        self._mode(mode)
        return await self.database.list_credentials()

    async def get_credential(self, filename, mode='antigravity'):
        self._mode(mode)
        return await self.database.get_credential(filename)

    async def store_credential(self, filename, data, mode='antigravity'):
        self._mode(mode)
        return await self.database.store_credential(filename, data)

    async def delete_credential(self, filename, mode='antigravity'):
        self._mode(mode)
        return await self.database.delete_credential(filename)

    async def update_credential_state(self, filename, updates, mode='antigravity'):
        self._mode(mode)
        # user_email no longer exists: email is the primary credential identity.
        updates = {key: value for key, value in updates.items() if key != 'user_email'}
        return await self.database.update_credential_state(filename, updates)


async def get_storage_adapter():
    # Lazy import permits the DB's concentrated write-guard decorator.
    from ..storage.database import get_database
    return StorageAdapter(await get_database())
