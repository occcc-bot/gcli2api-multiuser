"""Fail closed BEFORE upstream config/database initialization can alter schema.

This migration release supports a single process and the SQLite credential
backend only. PostgreSQL is rejected explicitly rather than using upstream's
initialization (which can drop legacy credential tables) to discover its schema.
No credential contents or unrelated files are read by this preflight.
"""
from contextlib import asynccontextmanager, closing
from functools import wraps
import os
from pathlib import Path
import sqlite3


def validate_worker_count():
    for name in ('WORKERS', 'WEB_CONCURRENCY'):
        try:
            workers = int(os.environ.get(name, '1'))
        except (TypeError, ValueError):
            raise RuntimeError(f'Multi-user extension requires {name}=1') from None
        if workers != 1:
            raise RuntimeError(f'Multi-user extension requires {name}=1; multi-process locks are not supported')
    return 1


def check_database_schema():
    """Inspect only credentials.db, without initializing the upstream database."""
    if os.environ.get('POSTGRESQL_URI', ''):
        raise RuntimeError('Multi-user migration currently supports SQLite only; PostgreSQL startup is disabled until a read-only schema preflight is implemented')
    path = Path(os.environ.get('CREDENTIALS_DIR', './creds')).expanduser().resolve() / 'credentials.db'
    if not path.exists():
        return  # Brand-new deployment: upstream may create its email-keyed DB.
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as database:
            database.execute('PRAGMA query_only=ON')
            columns = {row[1] for row in database.execute('PRAGMA table_info(antigravity_credentials)')}
            table_exists = database.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='antigravity_credentials'"
            ).fetchone() is not None
    except (OSError, sqlite3.Error):
        raise RuntimeError('Credential schema preflight failed; database was not initialized') from None
    if table_exists and ('filename' in columns or 'email' not in columns):
        raise RuntimeError('Legacy Antigravity credential schema detected; run the explicit filename-to-email migration before startup. Database was not modified')


class RuntimeLock:
    """Own a POSIX advisory lock until lifespan shutdown, without global leases."""
    def __init__(self, fd):
        self.fd = fd

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)  # Closing the descriptor also releases flock.

    def __del__(self):
        self.close()


def acquire_runtime_lock():
    try:
        import fcntl
    except ImportError:
        raise RuntimeError('Multi-user runtime requires POSIX fcntl; this deployment does not support Windows') from None
    directory = Path(os.environ.get('CREDENTIALS_DIR', './creds')).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = os.open(directory / 'multiuser-runtime.lock',
                     os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return RuntimeLock(fd)
    except OSError:
        if fd is not None:
            os.close(fd)
        raise RuntimeError('Multi-user runtime lock unavailable: only one service process may use a CREDENTIALS_DIR') from None


async def preflight():
    validate_worker_count()
    check_database_schema()
    return acquire_runtime_lock()


def guarded_lifespan(original):
    """Run preflight before upstream initialization; always release the lease."""
    @wraps(original)
    async def wrapped(app):
        lease = await preflight()
        try:
            async with asynccontextmanager(original)(app) as state:
                yield state
        finally:
            lease.close()
    return wrapped
