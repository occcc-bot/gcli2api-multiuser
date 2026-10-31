"""Compatibility import; implementation lives in src.multiuser.oauth."""
import sys
from .multiuser import oauth as _implementation
sys.modules[__name__] = _implementation
