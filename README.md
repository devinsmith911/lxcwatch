<img src="lxcwatch/static/icon.svg" alt="" width="64" height="64">

# lxcwatch

A web dashboard for keeping the LXCs in a Proxmox cluster up to date. For every container it shows the installed app version and the version available, pending package updates, and whether a newer Debian release is out. You can update one container or all of them from the page, with an optional snapshot first so you can roll back.

It's built for containers created with the [Proxmox VE community-scripts](https://community-scripts.org), the ones with the `update` command inside. Other containers still get package tracking and updates.

## Features

- **One view of the whole cluster.** lxcwatch connects to your Proxmox nodes over SSH, with nothing installed in the containers. For each container it shows:
  - the installed and available app version, with links to the release notes;
  - pending package updates, with security updates flagged;
  - whether services need a restart to pick up updated libraries;
  - whether a newer Debian release is available.
- **Updates from the browser.** It runs the community-script `update`, upgrades packages, does Debian release upgrades (e.g. 12 → 13) or restarts the container, and streams the log live.
- **Update all.** It updates apps, packages or both across every container that needs it, one container at a time.
- **Snapshots and rollback.** It can take a snapshot before any update, and roll back with one click.
- **Notifications.** New updates can go to ntfy, Pushover or a webhook.
- **Nothing happens on its own.** Scans run on a schedule, but updates only run when you start them.

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

- **Upgrade:** `apt update && apt upgrade`. lxcwatch also lists its own update under Packages. Your config, SSH key and database are kept.
- **Remove:** `apt remove lxcwatch` keeps your config, key and data; `apt purge lxcwatch` deletes them too.
- **If `/opt/lxcwatch/venv` goes missing:** run `dpkg-reconfigure lxcwatch`. The package rebuilds that folder on every install and upgrade, so don't edit or delete it.

The [changelog](CHANGELOG.md) lists what changed in each version. It's also installed as `/usr/share/doc/lxcwatch/CHANGELOG.md`.

## Configuration

lxcwatch reads its settings from environment variables, which the package keeps in `/etc/lxcwatch.env`. Restart the service after changing them. See [.env.example](.env.example) for a commented template.

| Variable | Default | |
|---|---|---|
| `LXCWATCH_PVE_HOST` | required | Any cluster node |
| `LXCWATCH_PASSWORD` | required | Web UI password (user `admin` unless `LXCWATCH_USERNAME` is set) |
| `LXCWATCH_SSH_USER` | `root` | A non-root user needs passwordless `sudo` for `pct` and `pvesh` |
| `LXCWATCH_NODE_ADDRESSES` | — | `pve1=10.0.0.11,...` if the node addresses Proxmox reports aren't reachable |
| `LXCWATCH_SCAN_INTERVAL_MINUTES` | `360` | `0` disables scheduled scans |
| `LXCWATCH_SCAN_ON_STARTUP` | `overdue` | `overdue`: scan at startup only if the last scan is older than the interval; `always`; `never` |
| `LXCWATCH_REFRESH_PACKAGES` | `true` | Runs `apt-get update` / `apk update` during scans |
| `LXCWATCH_KEEP_SNAPSHOTS` | `3` | `lxcwatch-*` snapshots kept per container (`0` = keep all) |
| `LXCWATCH_GITHUB_TOKEN` | — | Recommended once you have more than about 15 apps: GitHub allows 60 requests/hour without a token. It's also passed to `update` inside the containers, so app updates don't hit the limit either |
| `LXCWATCH_NTFY_URL` / `_TOKEN` | — | ntfy topic URL |
| `LXCWATCH_PUSHOVER_TOKEN` / `_USER` | — | Pushover application token and user key. Long messages are cut to Pushover's 1024-character limit |
| `LXCWATCH_WEBHOOK_URL` | — | Receives a JSON POST: `{title, message, items: [{text, url, vmid}], url}` |
| `LXCWATCH_NOTIFY_PACKAGES` | `security` | `none`, `security` or `all` |
| `LXCWATCH_PUBLIC_URL` | — | lxcwatch's address, linked from ntfy and Pushover notifications |

A GitHub token only needs public read access: a fine-grained token with no extra permissions works.

## Things to know

- **Security:** the SSH key is effectively root on your whole cluster, and so is anyone who can sign in to lxcwatch. Use a strong password and keep the UI off the internet.
- **Signing in:** sessions last 30 days, and changing `LXCWATCH_PASSWORD` signs everyone out. After 10 failed sign-ins from one address, that address is locked out for 15 minutes. Scripts can use HTTP basic auth instead, e.g. `curl -u admin:… https://lxcwatch.example/api/containers`. Behind a reverse proxy, pass `X-Forwarded-Proto` so the session cookie is marked secure.
- **Snapshots** need snapshot-capable storage (ZFS, LVM-thin, Ceph) and are off by default; tick "Take a snapshot first" when updating. If a snapshot can't be taken, the update doesn't run. Rolling back restores the whole container, including app data written since the snapshot.
- **Restarts:** containers share the host's kernel, so package updates never need a reboot for a new kernel. Services do keep running the old libraries until they restart, and lxcwatch shows **restart needed** when that's the case. Package updates can restart the container afterwards: only if needed (the default), always, or never.
- **Debian release upgrades** run one major version at a time from a container's Details panel, never as part of Update all. Before starting, lxcwatch checks disk space, held packages, third-party repositories and whether your Proxmox host supports the new version. Only the official Debian sources are switched; third-party repositories stay on the old release. A snapshot is ticked by default; if an upgrade fails part-way, roll back to it.
- **Apps installed from apt repositories** (Jellyfin, Pi-hole, AdGuard and similar) have no separate app version; their updates show up under Packages.
- **"Untracked"** means lxcwatch found a version file that the app's script doesn't refer to. **"Unknown"** means the installed or available version couldn't be determined.

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
