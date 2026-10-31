"""Offline test imports and execution; ASGI and SQLite stay in-process.

Import this BEFORE src modules: upstream src.utils performs an import-time
GitHub version lookup. Mock that documented HTTP boundary before it can run.
The global socket guard also covers collection, not only individual test bodies.
"""
import socket
from unittest.mock import patch

import httpx

_import_http_guard = patch('httpx.get', side_effect=httpx.ConnectError('Offline regression suite'))
_import_http_guard.start()
_collection_guards = []
for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo'):
    guard = patch(target, side_effect=AssertionError('Network is forbidden in regression tests'))
    guard.start()
    _collection_guards.append(guard)


def forbid_network(case):
    """Reassert guards locally, including if another test temporarily patched one."""
    for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo'):
        guard = patch(target, side_effect=AssertionError('Network is forbidden in regression tests'))
        guard.start()
        case.addCleanup(guard.stop)
