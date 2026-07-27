# late-deployd tests

Smoke and unit tests for the deploy webhook receiver. They run without systemd,
without `/root`, and without a real `/var/www/html`. Every state effect is
stubbed with `tmp_path` and `monkeypatch`.

## Layout

- `test_signature.py` — HMAC signature verification (the GitHub webhook auth).
- `test_config.py` — YAML config loader + hot-reload on mtime.
- `test_events.py` — In-process event bus + SQLite retention.
- `test_scheduler.py` — Async queue, per-repo lock, `_www_lock` for www writes.
- `test_deployers_unit.py` — `git_pull`, `ensure_repo`, deployer dispatch.
- `test_main_smoke.py` — FastAPI `TestClient` against the whole webhook surface.

## Run

```bash
cd services/deployd
python3 -m pytest tests/ -v
```

No pytest plugins beyond `pytest-asyncio` (already pulled in by `fastapi`'s
test stack). No external services required.

## Add new tests

- Pure sync logic → `test_<module>_unit.py`.
- Async / event-loop logic → `test_<module>.py` with `@pytest.mark.asyncio`.
- HTTP surface → `test_main_smoke.py` (uses `fastapi.testclient.TestClient`).

## Style

Each test sets its own `monkeypatch` env vars (`GITHUB_WEBHOOK_SECRET`,
`LOG_DIR`, `DEPOYD_CONFIG`, `LATE_DASHBOARD_HISTORY_DIR`) before importing
modules, so individual tests don't bleed state into each other.
