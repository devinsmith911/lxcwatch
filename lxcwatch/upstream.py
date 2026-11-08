"""Work out what `update` would install, from the community-scripts ct script.

Each ct/<app>.sh declares its release checks like

    check_for_gh_release "paperless" "paperless-ngx/paperless-ngx"
    check_for_gh_release "Immich" "immich-app/immich" "${RELEASE}" "reason"

The first argument names the version file (~/.<app>, lowercased, spaces
removed), the second the upstream repo, and an optional third pins a version.
We read those, resolve simple variable assignments for pins, and ask the
forge for its latest release.
"""

import asyncio
import contextvars
import logging
import re
import shlex
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .config import Config

log = logging.getLogger(__name__)

# Set while a manual scan runs: cache entries older than this count as
# missing, so each repo/script is fetched fresh once per scan (and shared by
# every container in it). Scheduled scans leave it unset and use the cache.
FRESH_SINCE: contextvars.ContextVar[float | None] = contextvars.ContextVar("fresh_since", default=None)


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    """(1, 2, 3) for "1.2.3" or "1.2.3+dev4.gabc"; None if it isn't X.Y.Z."""
    core = (version or "").split("+", 1)[0].lstrip("vV")
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)

CHECK_RE = re.compile(r"\bcheck_for_(gh|codeberg|gl)_release\b(.*)")
ASSIGN_RE = re.compile(r"""^\s*(?:export\s+|local\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=("[^"$`]*"|'[^']*'|[^\s;#"'$`]+)\s*(?:#.*)?$""")
VAR_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}?$")
FORGES = {"gh": "github", "codeberg": "codeberg", "gl": "gitlab"}
DEAD_BASES = ("git.community-scripts.org", "tteck/Proxmox")
RETRY_DELAY = 2.0  # seconds before retrying a failed ct script fetch


@dataclass
class Check:
    forge: str
    app: str
    repo: str
    pin: str | None
    tag_prefix: str | None

    @property
    def key(self) -> str:
        return self.app.lower().replace(" ", "")


def strip_v(version: str | None) -> str | None:
    if version and len(version) > 1 and version[0] in "vV" and version[1].isdigit():
        return version[1:]
    return version


def _assignments(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = ASSIGN_RE.match(line)
        if m:
            value = m.group(2)
            if value[:1] in "\"'":
                value = value[1:-1]
            out.setdefault(m.group(1), value)
    return out


def _resolve(arg: str, env: dict[str, str]) -> str | None:
    """Expand a lone $VAR / ${VAR} / ${VAR:-default}; None if we can't."""
    if "$" not in arg:
        return arg
    m = VAR_RE.match(arg)
    if not m:
        return None
    if m.group(1) in env:
        return env[m.group(1)]
    return m.group(2)


def parse_checks(script: str) -> list[Check]:
    env = _assignments(script)
    checks: list[Check] = []
    seen: set[tuple[str, str]] = set()
    for line in script.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = CHECK_RE.search(line)
        if not m or m.group(2)[:1] not in (" ", "\t"):
            continue
        rest = re.split(r";|&&|\|\||\bthen\b", m.group(2), maxsplit=1)[0]
        try:
            args = shlex.split(rest)
        except ValueError:
            continue
        if len(args) < 2:
            continue
        app, repo = _resolve(args[0], env), _resolve(args[1], env)
        if not app or not repo or "/" not in repo:
            continue
        pin = _resolve(args[2], env) if len(args) > 2 and args[2] else None
        prefix = _resolve(args[4], env) if len(args) > 4 and args[4] else None
        if (app, repo) in seen:
            continue
        seen.add((app, repo))
        checks.append(Check(FORGES[m.group(1)], app, repo, pin or None, prefix or None))
    return checks


FORGE_WEB = {"github": "https://github.com", "codeberg": "https://codeberg.org", "gitlab": "https://gitlab.com"}


def repo_links(forge: str, repo: str, tag: str | None) -> dict:
    """Browser links for a repo, its releases and (if known) one release."""
    base = f"{FORGE_WEB[forge]}/{repo}"
    releases = f"{base}/-/releases" if forge == "gitlab" else f"{base}/releases"
    release = None
    if tag:
        release = f"{releases}/{quote(tag, safe='')}" if forge == "gitlab" else f"{releases}/tag/{quote(tag, safe='')}"
    return {"repo_url": base, "releases_url": releases, "release_url": release}


def compare_url(forge: str, repo: str, old_tag: str, new_tag: str) -> str | None:
    if forge == "gitlab":
        return f"{FORGE_WEB[forge]}/{repo}/-/compare/{quote(old_tag, safe='')}...{quote(new_tag, safe='')}"
    return f"{FORGE_WEB[forge]}/{repo}/compare/{quote(old_tag, safe='')}...{quote(new_tag, safe='')}"


def releases_between(releases: list[dict], installed: str | None, target: str | None) -> dict:
    """Releases newer than `installed` up to and including `target` (newest first)."""
    tags = [strip_v(r["tag"]) for r in releases]
    start = tags.index(strip_v(target)) if target and strip_v(target) in tags else 0
    end = tags.index(strip_v(installed)) if installed and strip_v(installed) in tags else None
    if end is not None and end < start:
        return {"releases": [], "complete": True, "installed_tag": releases[end]["tag"]}
    picked = releases[start:end] if end is not None else releases[start:]
    return {
        "releases": picked,
        # False when the installed version is older than everything we fetched.
        "complete": end is not None,
        "installed_tag": releases[end]["tag"] if end is not None else None,
        "target_tag": releases[start]["tag"] if releases else None,
    }


def looks_like_version(value: str | None) -> bool:
    """A single token with a digit in it; rules out marker files like ~/.hushlogin."""
    return bool(value) and not any(c.isspace() for c in value) and any(c.isdigit() for c in value)


def build_components(checks: list[Check], versions: dict[str, str], latest: dict[tuple[str, str], str | None]) -> list[dict]:
    """Join release checks with the version files found in the container."""
    versions = {p: v for p, v in versions.items()
                if looks_like_version(v) and not p.rsplit("/", 1)[-1].startswith(".install-")}
    by_key = {path.rsplit("/", 1)[-1].lstrip("."): v for path, v in versions.items() if path.startswith("/root/.")}
    used: set[str] = set()
    components = []
    for i, c in enumerate(checks):
        installed = strip_v(by_key.get(c.key))
        upstream_tag = latest.get((c.forge, c.repo))
        upstream = strip_v(upstream_tag)
        if c.tag_prefix and upstream and upstream.startswith(c.tag_prefix):
            upstream = strip_v(upstream[len(c.tag_prefix):])
        target = strip_v(c.pin) or upstream
        missing = c.key not in by_key and c.key + ".pending" not in by_key
        # No version file: skip optional extras (Immich's VectorChord) this
        # container never installed, but keep the script's main app (its first
        # check). That one is missing after e.g. a Jellyseerr -> Seerr
        # migration, until the first update records ~/.seerr.
        if missing and i > 0:
            continue
        used.update({c.key, c.key + ".pending"})
        if not installed or not target:
            state = "unknown"
        elif installed == target:
            state = "current"
        else:
            state = "outdated"
        target_tag = c.pin or upstream_tag
        components.append({
            "name": c.app,
            "repo": c.repo,
            "forge": c.forge,
            "installed": installed,
            "target": target,
            "target_tag": target_tag,
            "upstream": upstream,
            "pinned": bool(c.pin),
            "pending": (c.key + ".pending") in by_key,
            "version_file": f"/root/.{c.key}",
            "missing_version": missing,
            "state": state,
            **repo_links(c.forge, c.repo, target_tag),
            "upstream_url": repo_links(c.forge, c.repo, upstream_tag)["release_url"] if c.pin and upstream_tag else None,
        })
    # Version files with no matching check: still worth showing.
    for path, raw in sorted(versions.items()):
        name = path.rsplit("/", 1)[-1].lstrip(".")
        if name in used or name.endswith(".pending"):
            continue
        if path.startswith("/opt/"):
            name = name.removesuffix("_version.txt")
        components.append({
            "name": name, "repo": None, "forge": None, "installed": strip_v(raw),
            "target": None, "upstream": None, "pinned": False, "pending": False, "state": "untracked",
        })
    return components


FORGE_URL_RE = re.compile(r"^https?://(?:www\.)?(github\.com|gitlab\.com|codeberg\.org)/([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
FORGE_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "codeberg"}
# Packages whose Homepage field is a website rather than their source repo.
PACKAGE_REPOS = {
    "jellyfin": "jellyfin/jellyfin", "jellyfin-server": "jellyfin/jellyfin", "jellyfin-web": "jellyfin/jellyfin-web",
    "grafana": "grafana/grafana", "grafana-enterprise": "grafana/grafana", "influxdb2": "influxdata/influxdb",
    "caddy": "caddyserver/caddy", "nodejs": "nodejs/node", "syncthing": "syncthing/syncthing",
}
NOT_AN_APP_RE = re.compile(r"^lib|-(common|data|doc|dev|dbg)$")
DISTROS = {"debian": ("Debian", "https://packages.debian.org"), "ubuntu": ("Ubuntu", "https://packages.ubuntu.com")}


def deb_upstream_version(version: str | None) -> str | None:
    """The upstream part of a Debian version: "1:2.7.12-1~deb13" -> "2.7.12"."""
    if not version:
        return version
    v = version.split(":", 1)[1] if re.match(r"^\d+:", version) else version
    if "-" in v:
        v = v.rsplit("-", 1)[0]
    return re.split(r"[+~]", v, maxsplit=1)[0] or version


def package_components(pkgs: list[dict], names: list[str | None], pending: list[dict],
                       components: list[dict], os_id: str | None = None, codename: str | None = None) -> list[dict]:
    """Components for apps installed with apt, from the probe's package candidates.

    The app's own package is the one named after it (exact name, then
    <name>-server, then the shortest name starting with it). Containers with
    no other app versions fall back to what they installed from vendor repos.
    """
    pkgs = list({p["name"]: p for p in pkgs}.values())
    taken = {c["name"].lower().replace(" ", "") for c in components}
    names = [n.lower().replace(" ", "") for n in names if n and len(n) >= 3]

    def rank(p: dict) -> tuple | None:
        for n in names:
            if p["name"] == n:
                return (0, 0)
            if p["name"] == f"{n}-server":
                return (1, 0)
        if any(p["name"].startswith(n) for n in names):
            return (2, p["origin"] is None, len(p["name"]))
        return None

    related = sorted((rank(p), p["name"], p) for p in pkgs if rank(p) and p["name"] not in taken)
    if related:
        picked = [related[0][2]]
    elif not components:
        picked = [p for p in sorted(pkgs, key=lambda p: p["name"])
                  if p["origin"] and not NOT_AN_APP_RE.search(p["name"])][:5]
    else:
        picked = []
    upgrades = {p["name"]: p["to"] for p in pending}
    distro, packages_site = DISTROS.get(os_id or "", (None, None))
    out = []
    for p in picked:
        forge = repo = None
        m = FORGE_URL_RE.match(p.get("homepage") or "")
        if m:
            forge, repo = FORGE_HOSTS[m.group(1)], m.group(2)
        elif p["name"] in PACKAGE_REPOS:
            forge, repo = "github", PACKAGE_REPOS[p["name"]]
        target = upgrades.get(p["name"], p["version"])
        links = repo_links(forge, repo, None) if repo else {"repo_url": None, "releases_url": None, "release_url": None}
        out.append({
            "name": p["name"], "source": "apt", "origin": p["origin"] or distro,
            "repo": repo, "forge": forge, "installed": p["version"], "target": target, "target_tag": None,
            "upstream": None, "pinned": False, "pending": False,
            "state": "outdated" if p["name"] in upgrades else "current",
            # Debian versions carry epochs and revisions; release tags don't.
            "notes_installed": deb_upstream_version(p["version"]), "notes_target": deb_upstream_version(target),
            "homepage": p.get("homepage"),
            "package_url": f"{packages_site}/{codename}/{p['name']}" if packages_site and codename and not p["origin"] else None,
            **links,
        })
    return out


class RateLimited(httpx.HTTPError):
    pass


def error_text(e: httpx.HTTPError) -> str:
    """Readable text for a failed fetch. Timeouts often carry no message at all."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} {e.response.reason_phrase}".rstrip()
    if isinstance(e, RateLimited):
        return str(e)
    if isinstance(e, httpx.TimeoutException):
        return f"timed out ({type(e).__name__})"
    return f"{type(e).__name__}{f': {e}' if str(e) else ''}"


def _check(r: httpx.Response) -> None:
    """raise_for_status, with a useful message for GitHub's rate limit."""
    if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
        reset = r.headers.get("x-ratelimit-reset")
        when = time.strftime("%H:%M", time.localtime(int(reset))) if reset and reset.isdigit() else "later"
        raise RateLimited(f"GitHub API rate limit reached (resets {when}); set LXCWATCH_GITHUB_TOKEN to raise it")
    r.raise_for_status()


class Upstream:
    """Cached access to ct scripts, forge releases and script status."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "lxcwatch"})
        self._cache: dict[tuple, tuple[float, object]] = {}
        # (forge, repo, tag) -> publish date (ISO), noted whenever we read a release.
        self.published: dict[tuple[str, str, str], str] = {}

    async def close(self) -> None:
        await self.http.aclose()

    def _get(self, key: tuple, ttl: float):
        hit = self._cache.get(key)
        since = FRESH_SINCE.get()
        if hit and time.time() - hit[0] < ttl and (since is None or hit[0] >= since):
            return True, hit[1]
        return False, None

    def _put(self, key: tuple, value):
        self._cache[key] = (time.time(), value)
        return value

    def script_base(self, base: str | None) -> str:
        if not base or any(dead in base for dead in DEAD_BASES):
            return self.cfg.scripts_base
        return base.rstrip("/")

    async def ct_script(self, base: str | None, name: str) -> str | None:
        url = f"{self.script_base(base)}/ct/{name}.sh"
        key = ("script", url)
        hit, value = self._get(key, 6 * 3600)
        if hit:
            return value
        # raw.githubusercontent.com fails now and then. Without the script a
        # container has no release checks, and its app shows as untracked until
        # the next scan, so retry once and then fall back to the last good copy.
        for attempt in range(2):
            try:
                r = await self.http.get(url)
                if r.status_code == 404:
                    return self._put(key, None)
                r.raise_for_status()
                return self._put(key, r.text)
            except httpx.HTTPError as e:
                if attempt == 0:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                stale = self._cache.get(key)
                if stale and stale[1] is not None:
                    log.warning("fetching %s failed (%s); using the copy from %s", url, error_text(e),
                                time.strftime("%H:%M", time.localtime(stale[0])))
                    return stale[1]
                raise

    async def latest(self, forge: str, repo: str, tag_prefix: str | None = None) -> str | None:
        key = ("release", forge, repo, tag_prefix)
        hit, value = self._get(key, 3 * 3600)
        if hit:
            return value
        base, headers = self._api(forge, repo)
        tag = None
        if forge != "gitlab" and not tag_prefix:
            r = await self.http.get(f"{base}/releases/latest", headers=headers)
            if r.status_code == 200:
                rel = r.json()
                tag = rel.get("tag_name")
                if tag and rel.get("published_at"):
                    self.published[(forge, repo, tag)] = rel["published_at"]
            elif r.status_code != 404:
                _check(r)
        if tag is None:
            r = await self.http.get(f"{base}/releases", params={"per_page": 50}, headers=headers)
            _check(r)
            for rel in r.json():
                if rel.get("draft") or rel.get("prerelease") or rel.get("upcoming_release"):
                    continue
                name = rel.get("tag_name") or ""
                if tag_prefix and not name.startswith(tag_prefix):
                    continue
                tag = name
                when = rel.get("published_at") or rel.get("released_at")
                if when:
                    self.published[(forge, repo, tag)] = when
                break
        return self._put(key, tag)

    def _api(self, forge: str, repo: str) -> tuple[str, dict]:
        if forge == "github":
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if self.cfg.github_token:
                headers["Authorization"] = f"Bearer {self.cfg.github_token}"
            return f"https://api.github.com/repos/{repo}", headers
        if forge == "codeberg":
            return f"https://codeberg.org/api/v1/repos/{repo}", {}
        return f"https://gitlab.com/api/v4/projects/{repo.replace('/', '%2F')}", {}

    async def releases(self, forge: str, repo: str) -> list[dict]:
        """Recent stable releases, newest first, normalised across forges."""
        key = ("releases", forge, repo)
        hit, value = self._get(key, 3 * 3600)
        if hit:
            return value
        base, headers = self._api(forge, repo)
        r = await self.http.get(f"{base}/releases", params={"per_page": 50}, headers=headers)
        _check(r)
        out = []
        for rel in r.json():
            if rel.get("draft") or rel.get("prerelease") or rel.get("upcoming_release"):
                continue
            tag = rel.get("tag_name") or ""
            if forge == "gitlab":
                url = (rel.get("_links") or {}).get("self") or repo_links(forge, repo, tag)["release_url"]
                published = rel.get("released_at")
                body = rel.get("description") or ""
            else:
                url = rel.get("html_url") or repo_links(forge, repo, tag)["release_url"]
                published = rel.get("published_at") or rel.get("created_at")
                body = rel.get("body") or ""
            out.append({"tag": tag, "name": rel.get("name") or tag, "url": url, "published": published,
                        "notes": body[:6000], "notes_truncated": len(body) > 6000})
        return self._put(key, out)

    async def lxcwatch_latest(self) -> dict | None:
        """lxcwatch's own latest release: {"version", "url"} (cached 6 hours)."""
        if not self.cfg.releases_url:
            return None
        key = ("lxcwatch",)
        hit, value = self._get(key, 6 * 3600)
        if hit:
            return value
        try:
            r = await self.http.get(self.cfg.releases_url, timeout=10)
            r.raise_for_status()
            data = r.json()
            value = {"version": (data.get("tag_name") or "").lstrip("vV"), "url": data.get("html_url")}
        except (httpx.HTTPError, ValueError):
            return None  # try again next scan rather than caching a failure
        return self._put(key, value)

    async def script_state(self, slug: str) -> dict | None:
        """Whether community-scripts still maintains this script (e.g. 'deleted')."""
        key = ("state", slug)
        hit, value = self._get(key, 6 * 3600)
        if hit:
            return value
        try:
            r = await self.http.get(f"{self.cfg.website_url}/api/update-info", params={"slug": slug}, timeout=8)
            value = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            value = None
        return self._put(key, value)
