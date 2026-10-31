# Multi-user regression tests (AGY2API)

Run from the repository root with its Python requirements installed:

```sh
python -m unittest discover -s tests -p 'test_*.py'
# Or, with pytest installed:
python -m pytest -q tests
node --test tests/test_member_quota_ui.mjs
```

Tests use temporary account/credential SQLite databases or in-memory fixtures.
`no_network.py` rejects sockets/DNS during collection and execution, and mocks
upstream's import-time GitHub version lookup before importing `src` modules:
a missed HTTP/Google mock must fail, not contact a provider. No deployment credentials or production database is used.
The tests do not verify a live Google authorization or a deployed container.

## Coverage and migration decisions

- `test_multiuser.py`: retain invite transactions/concurrency, password/token
  hashing, login/session expiry/revocation, disabled accounts, API keys, personal
  HTTP schemas and ownership tombstones. The adapter fixture now exposes the
  email-database seam for duplicate-refresh-token checks. Verified email replaces
  the former generated `mu_<uuid>.json` credential ID; the public `filename` field
  is retained for compatibility. GeminiCLI uploads are now explicitly rejected.
- `test_multiuser_agy.py`: exercise the real SQLAlchemy email database, real
  selector/decorators, real session cache and real HTTP authentication. Cover
  concurrent users, missing/pending/deleted/disabled/cooled-down credentials,
  admin/orphan separation, identical prompt isolation in memory and fake Redis,
  poisoned cache rejection, per-owner sticky state, storage failure, deletion
  during selection, foreign refresh rejection and child-task context inheritance.
  Both actual upstream streaming and non-streaming retry loops are run against
  mocked HTTP transports, asserting that real prewarm child tasks and every retry
  retain their owner. Also cover HTTP streams/header variants/invalid keys,
  verified Google identity versus uploaded JSON, cross-owner/admin/duplicate-token
  takeover rejection, refresh/delete serialization, exact-task upload capabilities
  and a fixed historical SQLite account/key/session fixture that works without
  token reissue. Real administrator quota/test/verify HTTP handlers are paused
  after reading a member row, then allowed to finish after member deletion;
  direct upstream database writes must not resurrect that row.
- `test_multiuser_isolation.py`: replace deleted `CredentialManager` class tests
  with direct ASGI lifetime/error/WebSocket tests. Selection is covered more
  realistically by `test_multiuser_agy.py`, rather than maintaining a fake copy
  of the removed class. Remove obsolete GeminiCLI preview/credit assertions.
- `test_multiuser_oauth.py`: import the extension package; keep one-use owner-bound
  OAuth state, replay/expiry/callback validation, atomic claims, reservation before
  write, pending failure states and private-log tests. Provider exchanges are
  synthetic. Mock verified email does not embed the synthetic refresh token.
- `test_multiuser_quota.py`: keep session-only access, ownership-before-read,
  token/metadata preservation, disabled credentials, deletion during refresh or
  provider lookup, error redaction and output allowlisting. Adapt to AGY's
  `fetch_quota_info(access_token, project_id)` signature. Historical GeminiCLI
  ownership rows are inserted as legacy SQL fixtures, not via the removed mode.
- `test_multiuser_version.py`: test the extension version router rather than
  modifying the upstream version implementation. Check fork release `0.2.0`,
  AGY baseline, router precedence, JSON update URL, numerical release ordering,
  invalid metadata, HTTP failure and transport errors.
- `test_multiuser_startup.py`: the real web lifespan must reject filename-key
  databases before config loading or upstream schema initialization; compare the
  original database SHA256, table columns and data afterward. Reject multi-worker
  settings before even checking the database. New/email schemas are accepted
  without creating/mutating credential databases; only the permission-restricted
  runtime lock file is created, and a second simultaneous lease is rejected.
  Fresh offline subprocesses check OAuth environment configuration reaches the
  real Flow/token-exchange payload, and both empty/partial settings behave as
  documented. Cold-import subprocesses load `src.storage.database` then `web`,
  and the reverse order, guarding against import cycles hidden by pytest's cache.
- `test_member_quota_ui.mjs`: retain DOM-only quota rendering/escaping,
  percentages, accessibility and timezone tests against the personal page.
- `test_migrate_legacy.py`: separate migration-tool suite, maintained independently.

No old test is silently skipped to hide an incompatible upstream interface.
