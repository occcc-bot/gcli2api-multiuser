"""Multi-user extension boundary for unmodified AGY2API components.

Account DB schema remains compatible with the previous release. In ownership
records and HTTP responses ``filename`` now means canonical Google email.
Only Antigravity credentials are supported. See isolation.py for the mandatory
selection/authentication boundary and install() for the single application hook.
"""
from .accounts import *  # public compatibility API
from .accounts import _error, _mode, _store_for_path


def install(app):
    """Mount personal APIs/pages and reset identity for the entire ASGI lifetime."""
    from pathlib import Path
    from fastapi import Request
    from fastapi.responses import HTMLResponse
    from .isolation import OwnerContextMiddleware
    from .oauth import router as oauth_router
    from .quota import router as quota_router
    from .version import router as version_router

    app.add_middleware(OwnerContextMiddleware)
    app.include_router(version_router)
    app.include_router(router)
    app.include_router(oauth_router)
    app.include_router(quota_router)
    # Preserve old client base URLs while all canonical routes stay upstream's.
    from ..router.antigravity.openai import router as openai_router
    from ..router.antigravity.gemini import router as gemini_router
    from ..router.antigravity.anthropic import router as anthropic_router
    from ..router.antigravity.model_list import router as models_router
    for compatibility_router in (openai_router, gemini_router, anthropic_router, models_router):
        app.include_router(compatibility_router, prefix='/antigravity', include_in_schema=False)
    personal_page = Path(__file__).resolve().parents[2] / 'front' / 'users.html'

    @app.get('/users', include_in_schema=False)
    @app.get('/users/', include_in_schema=False)
    async def personal(request: Request):
        if not personal_page.is_file():
            from fastapi import HTTPException
            raise HTTPException(404, 'Personal page is not installed')
        # Legacy standalone page uses absolute /users URLs. Render a tiny base
        # substitution instead of forking its JS, including for strip-prefix
        # reverse proxies. The canonical OAuth redirect remains fixed loopback.
        import json
        import os
        prefix = (os.environ.get('PATH_PREFIX') or request.scope.get('root_path', '')).rstrip('/')
        page = personal_page.read_text(encoding='utf-8')
        page = page.replace("fetch('/users' + path", "fetch(" + json.dumps(prefix) + " + '/users' + path")
        return HTMLResponse(page, headers={'Cache-Control': 'no-store'})
