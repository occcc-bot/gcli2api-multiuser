"""Compatibility import; implementation lives in src.multiuser.quota."""
import sys
from .multiuser import quota as _implementation
sys.modules[__name__] = _implementation
