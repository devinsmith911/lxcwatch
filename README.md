# lxcwatch

A dashboard for the LXCs in a Proxmox cluster. For each container it shows the installed app version, the version `update` would install, and pending OS packages. You can run updates from the page, and can take a snapshot first so you can roll back.

It's built for containers created with the [community-scripts](https://community-scripts.org) (`update` inside the LXC). Other containers still get OS package tracking and updates.

## How it works

lxcwatch logs in to your Proxmox nodes over SSH. It needs nothing installed in the containers.

- **Discovery:** `pvesh get /cluster/resources` on any node lists every LXC and which node it's on. `/cluster/status` gives each node's address.
- **Scan:** for each running container, `pct exec <id> -- sh -c <probe>` reads:
  - which community-script it came from (`/usr/bin/update`)
  - the installed versions (`/root/.<app>`, the files the scripts write)
  - pending packages (`apt-get -s dist-upgrade` or `apk version`)
- **Target version:** lxcwatch fetches the container's `ct/<app>.sh`, finds its `check_for_gh_release "<app>" "<owner/repo>" [pin]` calls, and asks GitHub, Codeberg or GitLab for the latest release. If the script pins a version (Immich does), the pin is the target. That matches what `update` would actually do.
- **Update:**
  1. Writes a small job script to the node and starts it detached (`setsid nohup`).
  2. The job runs `pct snapshot` (if you ticked it), then either `pct exec <id> -- env PHS_SILENT=1 update` for an app update or `apt-get dist-upgrade` / `apk upgrade` for OS packages.
  3. lxcwatch streams the job's log to the browser.
  4. After a successful update it rescans the container and prunes old `lxcwatch-*` snapshots.

  Restarting lxcwatch doesn't interrupt a running update. It reattaches on startup.
- **Notify:** after a scheduled scan, anything new is sent to ntfy and/or a webhook, once per version.

Nothing updates automatically. Updates only run when you click one of the update buttons.

## Install (in its own LXC)

1. Create a small Debian 12/13 LXC (1 core, 512 MB is plenty). Then, as root inside it:

   ```sh
   git clone <this repo> /root/lxcwatch && /root/lxcwatch/deploy/install.sh
   ```

   The installer creates `/etc/lxcwatch.env` with a random web password and an SSH key, then prints the public key.

2. Authorize the key as root on the nodes. On most clusters `/root/.ssh/authorized_keys` is a symlink to `/etc/pve/priv/authorized_keys`, so adding it once covers every node. Check with `ls -l /root/.ssh/authorized_keys`. Otherwise add it on each node.

   To limit where the key can be used from, prefix it with the lxcwatch LXC's IP:

   ```
   from="10.0.0.50" ssh-ed25519 AAAA... lxcwatch@lxcwatch
   ```

3. Set `LXCWATCH_PVE_HOST` in `/etc/lxcwatch.env`, then run `systemctl enable --now lxcwatch`.

4. Open `http://<lxc-ip>:8080`, ideally behind your reverse proxy with HTTPS.

To upgrade lxcwatch: `git pull && deploy/install.sh`.

Docker works too: mount `/data`, put your key there, and set the same variables plus `LXCWATCH_SSH_KEY=/data/id_ed25519`.

## Configuration

All configuration comes from environment variables. See [.env.example](.env.example).

| Variable | Default | |
|---|---|---|
| `LXCWATCH_PVE_HOST` | required | Any cluster node |
| `LXCWATCH_PASSWORD` | required | Web UI password (user `admin` unless `LXCWATCH_USERNAME` is set) |
| `LXCWATCH_SSH_USER` | `root` | A non-root user needs passwordless `sudo` for `pct` and `pvesh` |
| `LXCWATCH_NODE_ADDRESSES` | — | `pve1=10.0.0.11,...` if the cluster IPs aren't reachable |
| `LXCWATCH_SCAN_INTERVAL_MINUTES` | `360` | `0` disables scheduled scans |
| `LXCWATCH_REFRESH_PACKAGES` | `true` | Runs `apt-get update` / `apk update` during scans |
| `LXCWATCH_KEEP_SNAPSHOTS` | `3` | `lxcwatch-*` snapshots kept per CT (`0` = never prune) |
| `LXCWATCH_GITHUB_TOKEN` | — | Recommended once you have more than about 15 apps (the GitHub limit is 60 requests/hour without a token). Also passed to `update` inside the containers, so app updates don't hit the limit either |
| `LXCWATCH_NTFY_URL` / `_TOKEN` | — | ntfy topic URL |
| `LXCWATCH_WEBHOOK_URL` | — | Receives a JSON POST: `{title, message, items: [{text, url, vmid}], url}` |
| `LXCWATCH_NOTIFY_PACKAGES` | `security` | `none`, `security` or `all` |

## Things to know

- **Security:** the SSH key is effectively root on the whole cluster, and so is anyone who can log in to the web UI. Keep the password strong and the UI off the internet. All POST and DELETE requests need an `X-Requested-With` header, so another site can't trigger an update through your browser.
- **Snapshots** need snapshot-capable storage (ZFS, LVM-thin, Ceph). Snapshots are off by default; tick "Take a snapshot first" in the update dialog to get one. If the snapshot fails, nothing is changed and the job fails. Containers whose bind mounts block snapshots fail the same way.
- **GitHub token in updates:** the token goes to the container's `update` through stdin, never on a command line, so it doesn't show up in `ps` or in the job log. On the node it's a root-only file in `/var/log/lxcwatch`, deleted as soon as the update exits.
- **Restarting after OS updates:** containers share the host kernel, so an upgrade never needs a reboot for a new kernel. Running services do keep using the old libraries (OpenSSL, libc and so on) until they restart. lxcwatch detects processes still running replaced code (marked "(deleted)" in `/proc/*/maps`) and shows **restart needed**. The OS update dialog can restart the container afterwards: only if needed (the default), always or never. Each container also has a Restart button (`pct reboot`).
- **Rolling back** restores the whole container, including app data written since the snapshot.
- **"Untracked"** means lxcwatch found a version file that no release check in the script refers to. **"Unknown"** means the installed or upstream version couldn't be determined.
- Apps installed from apt repositories (Jellyfin, Pi-hole, AdGuard and similar) have no GitHub release check. Their updates show up under OS packages.

## Development

```sh
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest            # the probe also runs against real Debian/Alpine images when Docker is available
LXCWATCH_PVE_HOST=pve1 LXCWATCH_PASSWORD=dev .venv/bin/uvicorn lxcwatch.app:app --reload
```
