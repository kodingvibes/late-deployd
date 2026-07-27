# Install / bootstrap on a fresh late.kodingvibes.com host.

The deploy daemon lives at `kodingvibes/late-deployd`. Once installed it
listens on `127.0.0.1:9200` behind nginx (`/deploy-webhook`) and pulls/builds
the 11 managed repos on push to `main`.

## One-shot bootstrap

From a machine that can SSH into the host as root:

```bash
git clone git@github.com:kodingvibes/late-deployd.git /root/late-deployd
cd /root/late-deployd
bash scripts/install.sh
```

`install.sh` does:

1. Installs Python deps (`pip install -r requirements.txt`).
2. Writes `/root/.deployd.env` if missing (the webhook secret is fetched via
   `gh secret` from the repo's `DEPLOY_WEBHOOK_SECRET` env var).
3. Writes `/root/.deployd/config.yaml` with the 11 managed repos.
4. Installs `/etc/systemd/system/late-deployd.service` and enables it.
5. `systemctl restart late-deployd`.

## Manual config

`/root/.deployd/config.yaml` is hot-reloaded on every webhook. Add a new repo
by appending it and pushing (no daemon restart needed):

```yaml
repos:
  late-new-micro:
    path: /root/late-new-micro
    branch: main
    url: git@github.com:kodingvibes/late-new-micro.git
    type: micro
    micro_name: newmicro
    build_script: /root/late.kodingvibes.com/scripts/build-micro-newmicro.sh
    rebuild_shell: true
```

Wire the GitHub webhook for the new repo:

```bash
gh api -X POST repos/kodingvibes/late-new-micro/hooks \
  -f config[url]='https://late.kodingvibes.com/deploy-webhook' \
  -f config[content_type]=json \
  -f config[secret]=$(cat /root/.deployd.env | grep GITHUB_WEBHOOK_SECRET | cut -d= -f2) \
  -f events[]=push
```

## Run the test suite

```bash
cd /root/late-deployd
python3 -m pytest tests/ -v
```

Pure stdlib + fastapi + httpx + pyyaml + pytest + pytest-asyncio. No system
state touched.
