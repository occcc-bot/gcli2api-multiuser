"""Session-only quota lookup for one actively owned Antigravity credential.

Include ``router`` separately; legacy panel quota handlers are unchanged. Never
use the credential selector/pool here, including on refresh or upstream failure.
"""

import asyncio
from contextvars import ContextVar
import math

from fastapi import APIRouter, Depends

from log import log
from . import google_oauth_api
from .api import antigravity
from .api.antigravity import fetch_quota_info
from .google_oauth_api import Credentials
from .multiuser import _error, get_multiuser_store, require_member
from .storage_adapter import get_storage_adapter

router = APIRouter(prefix='/users/credentials', tags=['member-quota'])
_private_quota = ContextVar('member_quota_private', default=False)


class _PrivateQuotaLogger:
    """Suppress legacy helpers' raw exception/response logs in this task only."""

    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        method = getattr(self.original, name)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            if not _private_quota.get():
                return method(*args, **kwargs)
        return call


# Independent of member_oauth import order/configuration and scoped across awaits.
google_oauth_api.log = _PrivateQuotaLogger(google_oauth_api.log)
antigravity.log = _PrivateQuotaLogger(antigravity.log)


async def _require_active(user_id, filename, mode):
    try:
        names = await asyncio.to_thread(
            get_multiuser_store().owned_filenames, user_id, mode, [filename])
    except Exception:
        _error(503, '凭证存储暂不可用')
    if filename not in names:
        _error(404, '凭证不存在')


def _quota_models(result):
    """Allow only typed public model quota fields, never arbitrary upstream data."""
    if (not isinstance(result, dict) or result.get('success') is not True
            or not isinstance(result.get('models'), dict)):
        raise ValueError()
    models = {}
    for model_id, quota in result['models'].items():
        if not isinstance(model_id, str) or not isinstance(quota, dict):
            raise ValueError()
        remaining = quota.get('remaining')
        reset, raw = quota.get('resetTime'), quota.get('resetTimeRaw')
        if (type(remaining) not in (int, float) or not math.isfinite(remaining)
                or not isinstance(reset, str) or not isinstance(raw, str)):
            raise ValueError()
        models[model_id] = {'remaining': remaining, 'resetTime': reset,
                            'resetTimeRaw': raw}
    return models


@router.get('/{filename}/quota')
async def credential_quota(filename: str, mode: str = 'antigravity',
                           user: dict = Depends(require_member)):
    if mode != 'antigravity':
        _error(400, '仅支持 Antigravity 凭证')
    await _require_active(user['id'], filename, mode)
    try:
        adapter = await get_storage_adapter()
        data = await adapter.get_credential(filename, mode=mode)
    except Exception:
        _error(503, '凭证存储暂不可用')
    await _require_active(user['id'], filename, mode)
    if data is None:
        _error(404, '凭证不存在')
    privacy = _private_quota.set(True)
    try:
        try:
            credentials = Credentials.from_dict(data)
            refreshed = await credentials.refresh_if_needed()
            token = credentials.access_token
            if not isinstance(token, str) or not token.strip():
                raise ValueError()
        except Exception:
            await _require_active(user['id'], filename, mode)
            log.warning('成员凭证额度查询失败')
            _error(502, '额度查询失败，请稍后重试')
        # A deletion while refreshing must never recreate the adapter record.
        await _require_active(user['id'], filename, mode)
        if refreshed:
            try:
                updated = {**data, **credentials.to_dict()}
                updated['token'] = updated['access_token'] = token
                if not await adapter.store_credential(filename, updated, mode=mode):
                    raise RuntimeError()
            except Exception:
                _error(503, '凭证存储暂不可用')
        await _require_active(user['id'], filename, mode)
        try:
            result = await fetch_quota_info(token)
        except Exception:
            # Recheck even when upstream failed; a deleted record stays unknown.
            await _require_active(user['id'], filename, mode)
            log.warning('成员凭证额度查询失败')
            _error(502, '额度查询失败，请稍后重试')
        await _require_active(user['id'], filename, mode)
        try:
            models = _quota_models(result)
        except Exception:
            log.warning('成员凭证额度查询失败')
            _error(502, '额度查询失败，请稍后重试')
        return {'success': True, 'filename': filename, 'models': models}
    finally:
        _private_quota.reset(privacy)
