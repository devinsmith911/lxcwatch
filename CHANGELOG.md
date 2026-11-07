# Changelog

## 2.4.0 (2026-11-07)

### Added
- **Columns** in the toolbar chooses which columns the container table shows: Container, Node, Tags, App, Packages, Auto updates, Uptime and Scanned. Your choice is remembered in this browser, and **Reset to default** brings back the usual set.
- A new **Uptime** column shows how long each running container has been up. It's off by default.
- Notifications can be set up from the dashboard, under **Notifications** in the menu, instead of with environment variables: ntfy, Pushover, Slack and webhook addresses, which package updates to report, and lxcwatch's own address for links. **Send a test** checks each channel and shows what went wrong. Tokens you save aren't shown again. Anything set with an environment variable still works and appears read-only.
- Slack notifications, through an incoming webhook. Set it under **Notifications** or with `LXCWATCH_SLACK_WEBHOOK_URL`.

### Fixed
- A notification address that redirects (for example `http://` to `https://`) is now reported as failing. lxcwatch used to follow the redirect, which turned the message into an empty GET request and counted it as sent.

### Changed
- The menu's **Settings** is now **Automatic updates**, since that's all it holds now that notifications have their own dialog. Buttons that open a dialog no longer end in "…".
- Node, tags and automatic updates now have their own columns instead of sitting under the container name. The Auto updates column names the policy (Security, Packages or App + packages) rather than just saying "auto".

## 2.3.3 (2026-11-04)

### Added
- `LXCWATCH_IGNORE_NODES` leaves nodes and their containers out of lxcwatch, for example `LXCWATCH_IGNORE_NODES=pve3`. lxcwatch won't connect to those nodes, and it forgets their containers' scan results and automatic update settings, so turn automatic updates back on if you stop ignoring a node.

## 2.3.2 (2026-11-03)

### Fixed
- Behind a reverse proxy such as Zoraxy, dashboard requests could occasionally fail with an error like HTTP 521 even though lxcwatch was running. lxcwatch now keeps idle connections open for 75 seconds instead of 5, so the proxy no longer reuses one just as it closes. If you added `--timeout-keep-alive` yourself with a systemd override, you can remove it.

## 2.3.1 (2026-11-03)

### Changed
- **Sign out** is hidden while you're signed in through Cloudflare Access. It used to end your Access session, which signed you out of every Access application rather than just lxcwatch.

## 2.3.0 (2026-11-03)

### Changed
- The dashboard was rebuilt on a small UI library. It looks and works the same, but the page now updates only what changed instead of redrawing, so refreshes no longer disturb what you're doing.
- Keyboard focus stays where it is during refreshes and returns to your button when you close the side panel.
- The **Update all** dialog keeps its list current while it's open.
- A failed refresh now stays visible until a refresh succeeds.

### Fixed
- After a failed release upgrade pre-check, the dialog's button no longer shows text left over from an earlier check.

## 2.2.0 (2026-11-03)

### Added
- Sign in through Cloudflare Access, off until you turn it on. Enable it and set your team name, the application's audience tag and the allowed email addresses, and those people go straight to the dashboard without the sign-in page. A badge in the header shows when you're signed in this way, and the menu shows the email. **Sign out** also ends the Access session. The README's **Things to know** section has the settings.

## 2.1.0 (2026-11-03)

### Changed
- lxcwatch now lives on GitHub, and the update check and **Release notes** link point there. Installs from before this version need to switch to the new apt repository to keep getting upgrades; the README's **Upgrading and removing** section has the commands.

## 2.0.1 (2026-11-03)

### Changed
- The menu at the top right now opens from a settings cog instead of three dots, and has a cleaner layout with properly spaced items.

## 2.0.0 (2026-10-30)

### Added
- Automatic updates, off until you turn them on. Choose a maintenance window, the days it runs and how long new app releases wait (3 days by default) in the new **Settings** screen. Then choose per container what gets installed: security packages, all packages, or the app and packages. Updates run one container at a time with a snapshot first. A failure pauses that container's automatic updates and can roll back to the snapshot if you opt in. You get a summary notification after each run.

## 1.3.1 (2026-10-30)

### Fixed
- "Scan now" also checks for a new lxcwatch release straight away. Before, the "update available" badge could take up to 6 hours to appear.

## 1.3.0 (2026-10-30)

### Fixed
- Package sources that fail to refresh (for example a vendor repo whose signing key changed) are now reported. The container shows "lists stale" and appears under Problems, and Details names each failing source and why. Before, it looked up to date.
- After upgrading lxcwatch, the header no longer keeps offering the version you just installed.

## 1.2.0 (2026-10-29)

### Added
- Apps installed with apt (Jellyfin, Plex, Grafana, cloudflared and others from a vendor repo, or a distro package named after the app) now show their installed version, any pending upgrade, and links to the project. Where the source repo is known, "What's new" shows the release notes in between.

### Changed
- "Hide unknown" is now "Hide non-scripts", which hides only containers that weren't installed with community-scripts. Community-script containers whose version can't be determined have their own "Unknown version" box instead.
- A tidier details panel: an ✕ close button that stays put on phones, and the action buttons laid out in a grid there.

## 1.1.1 (2026-10-27)

### Changed
- Visual tweaks: a tidier top bar, and clicking outside the side panel closes it.

## 1.1.0 (2026-10-26)

### Added
- **lxcwatch tells you when it has an update.** A badge in the header (e.g. **1.1.0 available**) links to the release notes, and ntfy, Pushover or the webhook get one notification per new version, whatever `LXCWATCH_NOTIFY_PACKAGES` is set to. Turn it off with `LXCWATCH_UPDATE_CHECK=false`.
- **Activity:** the new **Activity** button lists recent jobs across all containers: when, which container, what kind, the result and how long it took. Filter by running, succeeded or failed, and open any job's log.
- **Manual snapshots:** **Take snapshot…** in a container's Details panel snapshots it at any time, running or stopped, whether or not updates are pending. Choose a name (prefilled as `manual-<date>-<time>`) and an optional description. Manual snapshots are never removed by the automatic cleanup after updates.

### Changed
- **Scan now** and **Rescan** fetch fresh release information and scripts instead of using cached data up to 3 hours old, so a release that just came out shows up right away. Scheduled scans still use the cache to stay within GitHub's rate limit.

## 1.0.0 (2026-10-23)

First stable release. It's the same code as 0.1.36; from here, version numbers follow the usual major.minor.patch meaning.

## 0.1.36 (2026-10-23)

### Added
- **Pushover notifications:** set `LXCWATCH_PUSHOVER_TOKEN` (an application token) and `LXCWATCH_PUSHOVER_USER` (your user key). You get the same notifications as ntfy, each announced once, cut to Pushover's 1024-character limit when there are many.

## 0.1.35 (2026-10-23)

### Changed
- Each version now appears under the repository's **Releases** in the self-hosted git server, with its changelog notes and the `.deb` attached for download.

## 0.1.34 (2026-10-23)

### Changed
- Releases are now cut from version tags instead of every push to `main`, so new versions arrive only when deliberately released. lxcwatch itself is unchanged.

## 0.1.33 (2026-10-23)

### Added
- **Details shows how long the container has been running**, and when it last started, e.g. "Running for 3d 4h · started 20 Oct, 16:05". If the last job (update, restart, release upgrade or rollback) restarted the container, it says so. The start time comes from Proxmox's uptime and refreshes with each scan.

## 0.1.32 (2026-10-23)

### Changed
- The scan info next to **Scan now** shows only the last and next scan time. The container count and scan duration were dropped; on mobile they were always cut off.

## 0.1.30 (2026-10-23)

### Changed
- **Tidier header:** the logo and version are on the left; the connection badge, theme button and **Sign out** are on the right.
- **Scan now** moved next to **Update all…**, with the last and next scan time beside it.
- **The stat boxes are the filters now:** click Containers, App updates, Package updates, Security or Problems to filter the table, and click again to show everything. The separate filter buttons are gone, and Hide stopped / Hide unknown are small chips.
- **Stat counts are per container:** App updates and Security count containers, so each number matches what the filter shows.
- **Update buttons only appear when needed:** the table shows **Update app** and **Update packages** only when there's something to update. **Details** still has both for a manual run.
- **Mobile:**
  - the stat boxes are one compact row that scrolls sideways;
  - table rows are compact cards (name, node and scan time, then app, then packages, then actions), so about five fit on screen;
  - the connection badge shortens to the node count.

### Fixed
- On mobile, the selected stat box's outline was cut off at the top. Selecting a box that's scrolled out of view now scrolls it into view.

## 0.1.26 (2026-10-23)

### Changed
- The install instructions now install `curl` first, since minimal containers often don't have it. They also explain how to rebuild `/opt/lxcwatch/venv` (`dpkg-reconfigure lxcwatch`) if it goes missing.

## 0.1.24 (2026-10-22)

No changes to lxcwatch itself (CI only).

## 0.1.23 (2026-10-22)

First release as a Debian package (`apt install lxcwatch` from the package registry). Installing it over a `deploy/install.sh` install takes over the existing config, SSH key and database. This version includes everything below.

### Tracking
- Finds every LXC in the Proxmox cluster over SSH, with nothing installed in the containers.
- Recognises community-script containers from `/usr/bin/update` in its current, older and full-script-copy forms. It reads installed app versions from `/root/.<app>` and works out the version `update` would install, including pinned versions such as Immich's.
- Shows pending package updates (apt and apk), flagging security updates.
- Shows **restart needed** when services still run libraries or binaries an update replaced.
- Flags Debian containers behind the current stable release (e.g. **Debian 13 available**).
- Links each available update to its release notes. **What's new** lists every release between the installed and target versions.
- Reports community-script failures, retired scripts (such as Jellyseerr and Overseerr, now Seerr) and apps whose version is unknown.

### Updating
- **Update app** runs the community-script `update` unattended. If `LXCWATCH_GITHUB_TOKEN` is set, it passes the token along so updates don't hit GitHub's rate limit.
- **Update packages** runs `apt-get dist-upgrade` / `apk upgrade`. It can restart the container afterwards: only if needed, always or never.
- **Update all** updates apps, packages or both, one container at a time.
- **Upgrade to Debian N** does a release upgrade with pre-checks (disk space, held packages, third-party repos, Proxmox host support, nesting).
- Optional snapshot before each update, with one-click rollback and automatic cleanup of old snapshots.
- Updates run on the Proxmox node, so restarting lxcwatch never interrupts one. Live logs stream in the browser.

### Dashboard
- Light, dark and system themes; pagination; filtering by name, ID, node or tag.
- Sign-in page with 30-day sessions. HTTP basic auth still works for API clients.
- Scheduled scans follow the last scan across restarts (`LXCWATCH_SCAN_ON_STARTUP`).
- ntfy and webhook notifications for new updates, with release links.
