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


def build_components(checks: list[Check], versions: dict[str, str], latest: dict[tuple[str, str], str | None]) -> list[dict]:
    """Join release checks with the version files found in the container."""
    by_key = {path.rsplit("/", 1)[-1].lstrip("."): v for path, v in versions.items() if path.startswith("/root/.")}
    used: set[str] = set()
    components = []
    for c in checks:
        installed = strip_v(by_key.get(c.key))
        upstream = strip_v(latest.get((c.forge, c.repo)))
        if c.tag_prefix and upstream and upstream.startswith(c.tag_prefix):
            upstream = strip_v(upstream[len(c.tag_prefix):])
        target = strip_v(c.pin) or upstream
        if installed is None and c.key not in by_key:
            # The app's version file doesn't exist, e.g. an optional
            # component (Immich's VectorChord) this container never installed.
            if c.key + ".pending" not in by_key:
                continue
        used.update({c.key, c.key + ".pending"})
        if not installed or not target:
            state = "unknown"
        elif installed == target:
            state = "current"
        else:
            state = "outdated"
        components.append({
            "name": c.app,
            "repo": c.repo,
            "forge": c.forge,
            "installed": installed,
            "target": target,
            "upstream": upstream,
            "pinned": bool(c.pin),
            "pending": (c.key + ".pending") in by_key,
            "state": state,
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
        if forge == "github":
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if self.cfg.github_token:
                headers["Authorization"] = f"Bearer {self.cfg.github_token}"
            base = f"https://api.github.com/repos/{repo}"
        elif forge == "codeberg":
            headers, base = {}, f"https://codeberg.org/api/v1/repos/{repo}"
        else:
            headers, base = {}, f"https://gitlab.com/api/v4/projects/{repo.replace('/', '%2F')}"
        tag = None
        if forge != "gitlab" and not tag_prefix:
            r = await self.http.get(f"{base}/releases/latest", headers=headers)
            if r.status_code == 200:
                tag = r.json().get("tag_name")
            elif r.status_code not in (404,):
                r.raise_for_status()
        if tag is None:
            r = await self.http.get(f"{base}/releases", params={"per_page": 50}, headers=headers)
            r.raise_for_status()
            for rel in r.json():
                if rel.get("draft") or rel.get("prerelease") or rel.get("upcoming_release"):
                    continue
                name = rel.get("tag_name") or ""
                if tag_prefix and not name.startswith(tag_prefix):
                    continue
                tag = name
                break
        return self._put(key, tag)

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
