<img src="lxcwatch/static/icon.svg" alt="" width="64" height="64">

# lxcwatch

A web dashboard for keeping the LXCs in a Proxmox cluster up to date. For every container it shows the installed app version and the version available, pending package updates, and whether a newer Debian release is out. You can update one container or all of them from the page, with an optional snapshot first so you can roll back.

It's built for containers created with the [Proxmox VE community-scripts](https://community-scripts.org), the ones with the `update` command inside. Other Debian, Ubuntu and Alpine containers still get package tracking and updates.

## Features

- **One view of the whole cluster.** lxcwatch connects to your Proxmox nodes over SSH, with nothing installed in the containers. For each container it shows:
  - the installed and available app version, with links to the release notes, including apps installed with apt such as Jellyfin or Plex;
  - pending package updates, with security updates flagged, and package sources that failed to refresh;
  - whether services need a restart to pick up updated libraries;
  - whether a newer Debian release is available.
- **Updates from the browser.** It runs the community-script `update`, upgrades packages, does Debian release upgrades (e.g. 12 → 13) or restarts the container, and streams the log live.
- **Update all.** It updates apps, packages or both across every container that needs it, one container at a time.
- **Snapshots and rollback.** It can take a snapshot before any update, or whenever you like, and roll back with one click.
- **Notifications.** New updates can go to ntfy, Pushover or a webhook. lxcwatch also tells you when there's a new version of itself.
- **Automatic updates, if you want them.** Off by default. Turn them on in **Settings**, pick the days and maintenance window, and choose per container what gets installed: security packages, all packages, or the app and packages.
- **Activity history.** See recent jobs across all containers, with their results and logs.

## Install

lxcwatch runs in its own small LXC (Debian 12/13 or Ubuntu 24.04; 1 core and 512 MB is plenty).

1. As root in that LXC, add the lxcwatch apt repository and install the package:

   ```sh
   apt install -y curl
   install -d -m 755 /etc/apt/keyrings
   curl -fsSL https://devinsmith911.github.io/lxcwatch/lxcwatch.asc -o /etc/apt/keyrings/lxcwatch.asc
   echo "deb [signed-by=/etc/apt/keyrings/lxcwatch.asc] https://devinsmith911.github.io/lxcwatch stable main" > /etc/apt/sources.list.d/lxcwatch.list
   apt update && apt install lxcwatch
   ```

   The package creates:
   - an `lxcwatch` user and its SSH key;
   - `/etc/lxcwatch.env`, with a random web password that's printed during install;
   - the `lxcwatch` systemd service.

   Its Python dependencies are bundled, so nothing is downloaded from PyPI.

2. Allow the SSH key to log in as root on your Proxmox nodes. The key is at `/var/lib/lxcwatch/id_ed25519.pub`. On most clusters `/root/.ssh/authorized_keys` is a symlink to `/etc/pve/priv/authorized_keys`, so adding the key once covers every node; check with `ls -l /root/.ssh/authorized_keys`. Otherwise add it on each node.

   To limit where the key can be used from, prefix it with the lxcwatch LXC's IP:

   ```
   from="10.0.0.50" ssh-ed25519 AAAA... lxcwatch@lxcwatch
   ```

3. Set `LXCWATCH_PVE_HOST` in `/etc/lxcwatch.env` to the address of any node, then run `systemctl restart lxcwatch`.

4. Open `http://<lxc-ip>:8080` and sign in as `admin` with the password from step 1. Ideally, put it behind your reverse proxy with HTTPS.

### Upgrading and removing

- **Upgrade:** `apt update && apt upgrade`. When a new version is out, a badge in the header links to its release notes. Your config, SSH key and database are kept.
- **Remove:** `apt remove lxcwatch` keeps your config, key and data; `apt purge lxcwatch` deletes them too.
- **If `/opt/lxcwatch/venv` goes missing:** run `dpkg-reconfigure lxcwatch`. The package rebuilds that folder on every install and upgrade, so don't edit or delete it.
- **Installed before the move to GitHub?** Those installs use an older apt repository that no longer gets new versions. Switch to the current one, then upgrade as usual:

  ```sh
  curl -fsSL https://devinsmith911.github.io/lxcwatch/lxcwatch.asc -o /etc/apt/keyrings/lxcwatch.asc
  echo "deb [signed-by=/etc/apt/keyrings/lxcwatch.asc] https://devinsmith911.github.io/lxcwatch stable main" > /etc/apt/sources.list.d/lxcwatch.list
  apt update && apt upgrade
  ```

The [changelog](CHANGELOG.md) lists what changed in each version. It's also installed as `/usr/share/doc/lxcwatch/CHANGELOG.md`.

## Configuration

lxcwatch reads its settings from environment variables, which the package keeps in `/etc/lxcwatch.env`. Restart the service after changing them. See [.env.example](.env.example) for a commented template.

| Variable | Default | |
|---|---|---|
| `LXCWATCH_PVE_HOST` | required | Any cluster node |
| `LXCWATCH_PASSWORD` | required | Web UI password |
| `LXCWATCH_USERNAME` | `admin` | Web UI username |
| `LXCWATCH_SSH_USER` | `root` | A non-root user needs passwordless `sudo` for `pct` and `pvesh` |
| `LXCWATCH_SSH_PORT` | `22` | SSH port on the Proxmox nodes |
| `LXCWATCH_SSH_KEY` | — | Path to the SSH private key. The package sets this for you |
| `LXCWATCH_NODE_ADDRESSES` | — | `pve1=10.0.0.11,...` if the node addresses Proxmox reports aren't reachable |
| `LXCWATCH_SCAN_INTERVAL_MINUTES` | `360` | `0` disables scheduled scans |
| `LXCWATCH_SCAN_ON_STARTUP` | `overdue` | `overdue`: scan at startup only if the last scan is older than the interval; `always`; `never` |
| `LXCWATCH_REFRESH_PACKAGES` | `true` | Runs `apt-get update` / `apk update` during scans |
| `LXCWATCH_KEEP_SNAPSHOTS` | `3` | `lxcwatch-*` snapshots kept per container (`0` = keep all) |
| `LXCWATCH_JOB_CONCURRENCY` | `2` | How many jobs can run at the same time |
| `LXCWATCH_GITHUB_TOKEN` | — | Recommended once you have more than about 15 apps: GitHub allows 60 requests/hour without a token. It's also passed to `update` inside the containers, so app updates don't hit the limit either |
| `LXCWATCH_NTFY_URL` / `_TOKEN` | — | ntfy topic URL |
| `LXCWATCH_PUSHOVER_TOKEN` / `_USER` | — | Pushover application token and user key. Long messages are cut to Pushover's 1024-character limit |
| `LXCWATCH_WEBHOOK_URL` | — | Receives a JSON POST: `{title, message, items: [{text, url, vmid}], url}` |
| `LXCWATCH_NOTIFY_PACKAGES` | `security` | `none`, `security` or `all` |
| `LXCWATCH_PUBLIC_URL` | — | lxcwatch's address, linked from ntfy and Pushover notifications |
| `LXCWATCH_UPDATE_CHECK` | `true` | Check for new lxcwatch releases: shows a badge in the header and sends one notification per new version |

A GitHub token only needs public read access: a fine-grained token with no extra permissions works.

## Things to know

- **Security:** the SSH key is effectively root on your whole cluster, and so is anyone who can sign in. Use a strong password and keep the UI off the internet.
- **Signing in:** sessions last 30 days, and 10 failed sign-ins lock an address out for 15 minutes. Scripts can use HTTP basic auth. Behind a reverse proxy, pass `X-Forwarded-Proto` so the session cookie is marked secure. `/healthz` needs no sign-in.
- **Snapshots** need snapshot-capable storage (ZFS, LVM-thin, Ceph); if one can't be taken, the update doesn't run. Rolling back restores the whole container, including app data written since the snapshot. Only automatic `lxcwatch-*` snapshots are cleaned up.
- **Restarts:** services keep running old libraries until they restart, shown as **restart needed**. Package updates can restart the container only if needed (the default), always, or never.
- **Debian release upgrades** run one major version at a time, never as part of Update all or automatic updates. lxcwatch runs pre-checks first and only switches official Debian sources, so third-party repositories stay on the old release.
- **Automatic updates** run only in your maintenance window and only for containers you opt in:
  - app updates always take a snapshot first, and package updates do too unless you turn that off;
  - new app releases wait 3 days by default;
  - containers that are stopped, busy or have a problem are skipped;
  - a failed update pauses that container until you resume it or a manual update succeeds;
  - the container running lxcwatch keeps its own package as is and isn't restarted.
- **Fresh data:** scheduled scans cache release data for a few hours to stay within GitHub's rate limit. **Scan now** and **Rescan** always fetch fresh data.
- **Status labels:** **Untracked** means a version file the app's script doesn't refer to. **Unknown** means the version couldn't be determined. **Lists stale** means a package source failed to refresh, so updates from it may be missing.

## How it works

- **Discovery:** `pvesh get /cluster/resources` on any node lists every LXC and its node; `/cluster/status` gives each node's address.
- **Scan:** for each running container, `pct exec` runs a read-only probe that reports:
  - which community-script created it, from `/usr/bin/update`;
  - the installed app versions, from the `/root/.<app>` files the scripts write;
  - pending packages;
  - processes still using replaced libraries.
- **Available version:** lxcwatch reads the container's script from the community-scripts repository, finds the GitHub, Codeberg or GitLab repo it installs from, and asks for the latest release. If the script pins a version (as Immich's does), that pin is what `update` installs, so lxcwatch shows the pin.
- **Updates** run as small job scripts started detached on the Proxmox node, so restarting lxcwatch never interrupts one. lxcwatch follows each job's log and rescans the container when it finishes.

## Building from source

To run lxcwatch from a checkout, for example to try a change:

```sh
git clone https://github.com/devinsmith911/lxcwatch.git && cd lxcwatch
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
LXCWATCH_PVE_HOST=pve1 LXCWATCH_PASSWORD=dev .venv/bin/uvicorn lxcwatch.app:app --reload
```

- **Tests:** with Docker available, they also run the container probe against real Debian and Alpine images.
- **Build the package yourself:** `packaging/build-deb.sh` needs `dpkg-deb`; Debian or Ubuntu works best. It writes the package to `dist/`.
- **Install without the apt repository:** `deploy/install.sh` sets up the same service from a checkout. It's for systems where you'd rather not use the package.
- **Docker:** build the image with `docker build -t lxcwatch .`. Mount a volume at `/data`, put the SSH key there, and set the same variables plus `LXCWATCH_SSH_KEY=/data/id_ed25519`.

Found a bug or have an idea? [Open an issue](https://github.com/devinsmith911/lxcwatch/issues).

## License

[MIT](LICENSE)
