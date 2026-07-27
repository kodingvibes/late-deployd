# late-deployd — auto-deploy webhook receiver.

GitHub webhook → queue of deploy jobs → 2-worker async pool that runs
`git pull`, builds the affected repo, copies/writes artifacts and reloads
nginx. Per-repo lock serializes the same repo; a global `_www_lock` serializes
the writes under `/var/www/html/` so two micros can never race the shell
replace.

This repo is **self-hosted**: a push to `main` triggers the deploy webhook
in `/root/.deployd/config.yaml`, which runs `scripts/self-update.sh`, which
re-pulls this repo and `systemctl restart late-deployd`.

See `tests/` for the test suite. Endpoints:

- POST `/deploy-webhook`  push event from GitHub → enqueue deploy
- GET  `/health`          liveness + configured repo list
- GET  `/logs`            recent deploy log filenames
- GET  `/api/deployd/events` recent events (filterable by `?repo=` and `?type=`)
- WS   `/api/deployd/events/ws` live stream (super_admin auth)
- GET  `/api/dashboard/state`  super_admin snapshot
- GET  `/api/dashboard/history` super_admin time-series samples
- WS   `/api/dashboard/ws`      super_admin gauge/feed
