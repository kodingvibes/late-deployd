# late-deployd — auto-deploy webhook receiver.

GitHub webhook → queue of deploy jobs → 2-worker async pool that runs
`git pull`, builds the affected repo, copies/writes artifacts and reloads
nginx. Per-repo lock serializes the same repo; a global `_www_lock` serializes
the writes under `/var/www/html/` so two micros can never race the shell
replace.

A repo poller (see `poller.py`) runs in the same process on a fixed
interval (default 600 s, set `DEPLOYD_POLL_INTERVAL` to override). It does
`git fetch --quiet origin` for every managed repo, compares `HEAD` with
`origin/<branch>`, and enqueues a deploy through the same `Scheduler` when
upstream moved. This closes the gap when GitHub webhooks are lost (replay
late, transient 5xx, NAT we can't see) — max staleness is one poll
interval plus the deploy duration.

This repo is **self-hosted**: a push to `main` triggers the deploy webhook
in `/root/.deployd/config.yaml`, which runs `scripts/self-update.sh`, which
re-pulls this repo and `systemctl restart late-deployd`.

See `tests/` for the test suite. Endpoints:

- POST `/deploy-webhook`  push event from GitHub → enqueue deploy
- GET  `/health`          liveness + configured repo list + poll interval; `?poll=1` triggers a tick
- GET  `/logs`            recent deploy log filenames
- GET  `/api/deployd/events` recent events (filterable by `?repo=` and `?type=`)
- WS   `/api/deployd/events/ws` live stream (super_admin auth)

Poller events (same `/api/deployd/events` endpoint, filtered by `?type=`):

- `poll.started`      poll loop starting a new cycle
- `poll.upstream_ahead`  upstream has commits we don't; a deploy was enqueued
- `poll.fetch_failed`    `git fetch origin` returned non-zero (slow upstream, auth, etc)
- `poll.finished`      poll cycle done; payload has `checked` / `triggered` counts
