"""Fork release metadata without modifying upstream's version implementation."""
import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix='/version', tags=['multiuser-version'])
ROOT = Path(__file__).resolve().parents[2]


def _version_tuple(value):
    try:
        parts = value.split('.')
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return None
        return tuple(map(int, parts))
    except (AttributeError, TypeError):
        return None


@router.get('/info')
async def get_version_info(check_update: bool = False):
    metadata = json.loads((ROOT / 'fork-version.json').read_text())
    release = metadata['release']
    result = {
        'success': True,
        'version': 'multiuser-' + release,
        'full_hash': metadata['upstream_commit'],
        'upstream_commit': metadata['upstream_commit'],
        'repository': metadata['repository'],
        'release': release,
        'message': metadata['message'],
        'date': '',
    }
    if check_update:
        from src.httpx_client import get_async
        url = ('https://raw.githubusercontent.com/' + metadata['repository']
               + '/refs/heads/main/fork-version.json')
        try:
            response = await get_async(url, timeout=10.0)
            if response.status_code != 200:
                result.update(check_update=False,
                              update_error=f'GitHub HTTP {response.status_code}')
            else:
                remote = json.loads(response.text)
                current_version = _version_tuple(release)
                latest_version = _version_tuple(remote.get('release'))
                if latest_version is None or current_version is None:
                    raise ValueError('Invalid release metadata')
                result.update(
                    check_update=True,
                    has_update=latest_version > current_version,
                    latest_version='multiuser-' + remote['release'],
                    latest_hash=remote.get('upstream_commit', ''),
                    latest_message=remote.get('message', ''),
                    latest_date='',
                )
        except Exception:
            result.update(check_update=False, update_error='Fork update check unavailable')
    return result
