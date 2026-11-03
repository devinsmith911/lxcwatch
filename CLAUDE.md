# lxcwatch

Web dashboard that tracks and updates the LXCs in a Proxmox cluster, built around the Proxmox VE community-scripts (`update` inside each container). Python 3.11+, FastAPI, SQLite, the system `ssh` client; the UI is one page in Preact, with no build step. Developed on GitHub (github.com/devinsmith911/lxcwatch). The public installs the Debian package from the apt repository on GitHub Pages; the README is written for those users.

## Layout

- `lxcwatch/app.py`: FastAPI routes, auth middleware, `_summary()` (what the UI gets per container), the scan scheduler.
- `lxcwatch/cluster.py`: discovery (`pvesh`), per-container scans, upstream resolution, snapshots, the self-update check.
- `lxcwatch/probe.py`: the POSIX `sh` probe run in each container via `pct exec`, and its parser. It must keep working on Debian, Ubuntu and Alpine (busybox).
- `lxcwatch/upstream.py`: parses `check_for_*_release` calls in community-scripts `ct/<app>.sh`, fetches forge releases, caches them, and builds the component list.
- `lxcwatch/jobs.py`: updates, restarts, rollbacks, snapshots and release upgrades. Each job is a bash script started detached on the node (`setsid nohup`), logging to `/var/log/lxcwatch/<id>.log`, followed over SSH until a `__LXCWATCH_EXIT__ <rc>` marker. After a restart lxcwatch re-attaches (`Jobs.resume`).
- `lxcwatch/batch.py`: Update all, one job at a time; also runs automatic-update plans.
- `lxcwatch/auto.py`: automatic updates: settings (kv `auto_settings`), the maintenance window, planning per container policy (`auto_policy` table), pausing after failures, the run summary.
- `lxcwatch/release.py`: Debian release upgrades (pre-checks, source switching).
- `lxcwatch/notify.py`: ntfy, Pushover and webhook notifications, each item once (the `notified` table).
- `lxcwatch/auth.py`: signed session cookies, the login lockout and Cloudflare Access JWT checks; basic auth still works for API clients.
- `lxcwatch/db.py`: SQLite. New columns are added in `DB.__init__` with `PRAGMA table_info` plus `ALTER TABLE`, so existing installs upgrade in place.
- `lxcwatch/static/index.html`: an empty page that loads `js/main.js`. `login.html` is the sign-in page.
- `lxcwatch/static/css/app.css`: the dashboard's styles.
- `lxcwatch/static/js/`: the UI, as Preact components written with `htm` and loaded as native ES modules: `core` (state, helpers, the rules for reading a container), `actions` (what buttons do: refresh, drawer, dialogs, starting jobs), `header`, `table` (the main view), `drawer` (job log, activity), `details`, `dialogs`, `main` (startup).
- `lxcwatch/static/vendor/`: `preact-htm.js`, the unmodified Preact and htm bundle, and its licences. There's no build step or Node dependency.
- `packaging/`: `build-deb.sh` and the maintainer scripts. `deploy/install.sh` is the from-source install.
- `dev/` (gitignored, local only; may not exist in a fresh clone): the fake Proxmox node and the mock-UI screenshot tools (see `dev/README.md` there).

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                  # Docker-based probe tests skip if Docker isn't running (here: Rancher Desktop)
dev/fakepve/run.sh                   # (local dev/ only) fake Proxmox node on :2222 for end-to-end runs
dev/screenshots.sh <dir> ['js']      # (local dev/ only) desktop and 390px mobile, light and dark, from mock data
packaging/build-deb.sh               # local .deb, versioned <last tag>+dev<n>.<sha>
```

## Things that bit before (keep them true)

- App updates run `env PHS_SILENT=1 TERM=xterm-256color update`. `TERM=dumb`, or no TERM at all, makes the scripts' `clear` exit 1 and kills the update.
- The GitHub token reaches `update` through stdin (`APP_CMD_TOKEN`), never through argv or the job script.
- In remote shell commands, a trailing `&` backgrounds the whole `&&` chain, which then reads stdin from `/dev/null`. Wrap it: `{ nohup … & }`.
- Version files (`/root/.<app>`) only count if their value is one token containing a digit. That filters out `.hushlogin`, and `.install-*.failed` files are failure flags, not versions.
- `/usr/bin/update` has three forms: `UPDATE_SCRIPT_NAME` exports, a legacy `…/ct/<name>.sh` URL, or a full copy of the ct script (name taken from `APP=`).
- Apps installed with apt are found from `/var/lib/apt/lists`: a repo whose Release `Origin` isn't Debian/Ubuntu is a vendor repo. Indexes can be stored compressed (`Packages.lz4`, the default in Docker's Debian image), so read them with `apt-helper cat-file`.
- Snapshots: automatic ones are `lxcwatch-*` and get pruned (`KEEP_SNAPSHOTS`); `manual-*` and any other name are never pruned.
- Manual scans set `upstream.FRESH_SINCE` (a context variable), so cached upstream data older than the scan is refetched once per scan. Scheduled scans use the cache.
- Every POST/DELETE needs the `X-Requested-With: lxcwatch` header (CSRF protection).
- uvicorn runs with `--timeout-keep-alive 75`. Its 5-second default matches the dashboard's 5-second poll, so a reverse proxy could reuse a connection just as uvicorn closed it. Requests reaching a Go proxy such as Zoraxy over HTTP/2 aren't retried, so they failed with a 521. Keep this above the proxy's idle timeout (Zoraxy's is 30 seconds).
- Headless Chrome won't go below a 500px viewport. Mobile checks go through a 390px iframe, which the local `dev/screenshots.sh` handles.

## Working conventions

- Commit and push straight to `main` (remote `origin` is GitHub). Pushes don't run CI, so run the tests first; releases follow `RELEASING.md` (plain `X.Y.Z` tags, no `v`). Only cut a release when asked.
- Add user-facing changes to `CHANGELOG.md` under `## Unreleased`, written for users rather than as commit summaries. Leave CI and internal changes out.
- Keep the README written for outside users: no maintainer or push instructions (those belong in `RELEASING.md`).
- For UI changes, render screenshots (desktop and mobile, light and dark) and check them before pushing. When the user asks to see them first, wait for approval before pushing.
- UI code: views read `state` and call `update()` after changing it. htm drops whitespace containing a newline, so use `${" "}` where a space between elements on separate lines matters. The bundled Preact sets a `<select>`'s `value` before its options exist, so mark the chosen `<option>` with `selected` instead. Values in `html` templates are escaped; never set `innerHTML`.
- UI style: no coloured stripes down the side of cards or rows. Use tints, status dots or badges. Show update buttons only when there's something to update. Say "Update packages", not "Update OS".
- Defaults stay conservative: automatic updates are off globally and per container until the user opts in, snapshots are opt-in for updates (but ticked by default for Debian release upgrades), and restarts after package updates default to "only if needed".
- Use `info@dsmithtech.com` for any contact or maintainer address in the repo, never a personal address.
- `.github/workflows/release.yml` runs on GitHub-hosted runners for version tags, testing and building in `debian:13`. Publishing to the apt repository needs the `APT_SIGNING_KEY` secret.
