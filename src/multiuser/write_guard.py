"""One storage write boundary for all upstream and personal credential writes.

No imports of the database or account modules at import time: this decorator is
used by Database.store_credential and must not introduce a circular import.
Deleted mappings are never writable. Pending mappings are writable only by an
explicit member-upload capability, bound to identity AND the exact current Task.
An inherited capability in a fire-and-forget task does not authorize a write.
The shared reentrant per-email lock covers ownership checking, SQL awaits and
commit, serializing writes with personal deletion in the supported single worker.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_upload_write = ContextVar('multiuser_upload_write', default=None)


@contextmanager
def member_upload_write(user_id, email):
    from .accounts import get_multiuser_store
    grant = (asyncio.current_task(), get_multiuser_store().path, user_id, email)
    token = _upload_write.set(grant)
    try:
        yield
    finally:
        _upload_write.reset(token)


def guarded_store(original):
    @wraps(original)
    async def wrapped(database, email, credential_data):
        from .accounts import get_multiuser_store, _error
        from .isolation import credential_lock
        async with credential_lock(email):
            try:
                store = get_multiuser_store()
                mapping = store.credential_mapping(email)
            except Exception:
                _error(503, 'Credential ownership storage unavailable')
            if mapping is not None:
                if mapping['state'] == 'pending':
                    expected = (asyncio.current_task(), store.path, mapping['user_id'], email)
                    if _upload_write.get() != expected:
                        return False
                elif mapping['state'] != 'active':
                    return False
            # The outer lock remains held until the upstream transaction exits
            # and commits, so deletion cannot interleave a stale token write.
            return await original(database, email, credential_data)
    return wrapped
