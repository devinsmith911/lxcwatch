"""Work out what `update` would install, from the community-scripts ct script.

Each ct/<app>.sh declares its release checks like

    check_for_gh_release "paperless" "paperless-ngx/paperless-ngx"
    check_for_gh_release "Immich" "immich-app/immich" "${RELEASE}" "reason"

The first argument names the version file (~/.<app>, lowercased, spaces
removed), the second the upstream repo, and an optional third pins a version.
We read those, resolve simple variable assignments for pins, and ask the
forge for its latest release.
"""

import re
import shlex
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .config import Config

CHECK_RE = re.compile(r"\bcheck_for_(gh|codeberg|gl)_release\b(.*)")
ASSIGN_RE = re.compile(r"""^\s*(?:export\s+|local\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=("[^"$`]*"|'[^']*'|[^\s;#"'$`]+)\s*(?:#.*)?$""")
VAR_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}?$")
FORGES = {"gh": "github", "codeberg": "codeberg", "gl": "gitlab"}
DEAD_BASES = ("git.community-scripts.org", "tteck/Proxmox")


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


class RateLimited(httpx.HTTPError):
    pass


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

    async def close(self) -> None:
        await self.http.aclose()

    def _get(self, key: tuple, ttl: float):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
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
        hit, value = self._get(("script", url), 6 * 3600)
        if hit:
            return value
        r = await self.http.get(url)
        if r.status_code == 404:
            return self._put(("script", url), None)
        r.raise_for_status()
        return self._put(("script", url), r.text)

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
                tag = r.json().get("tag_name")
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
