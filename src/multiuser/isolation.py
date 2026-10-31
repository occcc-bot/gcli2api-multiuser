"""Mandatory credential boundary, shared by all APIs and their retry tasks.

The two decorators are intentionally the only credential-manager hooks. Keep
upstream's selector source intact for easy merges; selection itself is replaced
here, since upstream random/sticky/cache state is not tenant-aware. Token refresh
continues to use upstream's implementation, guarded by ownership and a per-email
lock. Child tasks inherit ContextVars, including streaming/prefetch retries.
"""
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import hashlib
import random

from .accounts import (ADMIN_OWNER, ORPHAN_OWNER, current_owner,
                       get_multiuser_store, _error)
from .storage import private_operation

_sticky = {}
_locks = {}


def effective_owner():
    # None is allowed only outside an HTTP request (legacy internal admin jobs).
    return current_owner.get() or ADMIN_OWNER


class OwnerContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        token = current_owner.set(ORPHAN_OWNER)
        try:
            await self.app(scope, receive, send)
        finally:
            current_owner.reset(token)


@asynccontextmanager
async def credential_lock(email):
    # Reentrant only for the exact owning asyncio Task, NOT ContextVars: child
    # tasks inherit ContextVars but must still wait for the parent's write lock.
    # The DB write decorator nests under refresh/upload/quota operations.
    key = (get_multiuser_store().path, email)
    entry = _locks.setdefault(key, [asyncio.Lock(), 0, None])
    entry[1] += 1
    task = asyncio.current_task()
    acquired = False
    try:
        if entry[2] is not task:
            await entry[0].acquire()
            entry[2] = task
            acquired = True
        yield
    finally:
        if acquired:
            entry[2] = None
            entry[0].release()
        entry[1] -= 1
        if not entry[1]:
            _locks.pop(key, None)


def allowed_email(email, owner=None):
    owner = effective_owner() if owner is None else owner
    if owner == ORPHAN_OWNER:
        return False
    try:
        return email in get_multiuser_store().owned_filenames(owner, 'antigravity', [email])
    except Exception:
        _error(503, 'Credential ownership storage unavailable')


def scoped_session_key(owner, session_key):
    if not session_key:
        return None
    namespace = get_multiuser_store().path
    digest = hashlib.sha256(f'{namespace}\0{owner}\0{session_key}'.encode()).hexdigest()
    return f'mu:{digest}'


def isolated_selector(original):
    @wraps(original)
    async def wrapped(model_name=None, session_key=None):
        return await select_credential(model_name, session_key)
    return wrapped


def guarded_refresh(original):
    @wraps(original)
    async def wrapped(credential_data, email):
        async with credential_lock(email):
            if not allowed_email(email):
                return None
            privacy = private_operation.set(effective_owner() != ADMIN_OWNER)
            try:
                result = await original(credential_data, email)
                return result if allowed_email(email) else None
            finally:
                private_operation.reset(privacy)
    return wrapped


def reset_sticky():
    _sticky.clear()


async def select_credential(model_name=None, session_key=None):
    from .. import credential_manager as manager, session_cache
    from ..storage.database import get_database
    from config import get_credential_select_mode

    owner = effective_owner()
    if owner == ORPHAN_OWNER:
        return None
    try:
        database = await get_database()
        store = get_multiuser_store()
        names = store.owned_filenames(owner, 'antigravity', await database.list_credentials())
    except Exception:
        _error(503, 'Credential ownership storage unavailable')
    if not names:
        return None

    mode = await get_credential_select_mode()
    key = scoped_session_key(owner, session_key)
    sticky_key = (store.path, owner)
    preferred = None
    if mode == 'cache' and key:
        try:
            preferred = await session_cache.get_session_binding(key)
        except Exception:
            pass  # Losing affinity is harmless; losing ownership is not.
    elif mode == 'sticky':
        preferred = _sticky.get(sticky_key)
    if preferred not in names:
        preferred = None
    random.shuffle(names)
    if preferred:
        names.remove(preferred)
        names.insert(0, preferred)

    refresh_failures = 0
    for email in names:
        if not allowed_email(email, owner):
            continue
        result = await database.get_available_credential_by_email(email, model_name=model_name)
        if result is None:
            continue
        # Never trust a storage/cache backend to return the requested identity.
        if result[0] != email or not allowed_email(result[0], owner):
            continue
        final = await manager._validate_or_refresh(*result)
        if final is None:
            refresh_failures += 1
            if refresh_failures >= 3:
                break
            continue
        if final[0] != email or not allowed_email(email, owner):
            continue
        try:
            if mode == 'cache' and key:
                await session_cache.set_session_binding(key, email)
            elif mode == 'sticky':
                _sticky[sticky_key] = email
        except Exception:
            pass
        return final

    if mode == 'sticky':
        _sticky.pop(sticky_key, None)
    if mode == 'cache' and key:
        try:
            await session_cache.delete_session_binding(key)
        except Exception:
            pass
    return None
