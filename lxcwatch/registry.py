"""Newer images for apps run with docker compose.

The probe reports each container's image reference (nginx:1.27,
ghcr.io/immich-app/immich-server:release) and the digests its image was pulled
as. We ask the registry what that tag points at now: a HEAD on the manifest
returns its digest, after fetching an anonymous token if the registry asks for
one. If that digest isn't among the local ones, the tag has moved on and
`docker compose pull` would fetch a new image.

HEAD requests don't count towards Docker Hub's pull limit, so the check itself
is cheap. For the version numbers shown, Docker Hub's tag list says which
version tags share a digest; other registries get the image's
org.opencontainers.image.version label from its config.
"""

import hashlib
import logging
import re
import time
from dataclasses import dataclass

import httpx

from .upstream import FORGE_HOSTS, FORGE_URL_RE, Upstream, error_text, repo_links

log = logging.getLogger(__name__)

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')
VERSION_TAG_RE = re.compile(r"^v?\d+(\.\d+)*([.-][0-9A-Za-z.-]+)?$")
DIGEST_TTL = 3 * 3600
TAGS_TTL = 3 * 3600


@dataclass(frozen=True)
class ImageRef:
    registry: str  # docker.io, ghcr.io, lscr.io, registry.example.com:5000
    repo: str  # library/nginx, immich-app/immich-server
    tag: str | None
    digest: str | None  # set when the reference pins one (image@sha256:...)

    @property
    def api(self) -> str:
        return "https://" + ("registry-1.docker.io" if self.registry == "docker.io" else self.registry) + "/v2/" + self.repo


def parse_ref(ref: str | None) -> ImageRef | None:
    """Split an image reference the way docker does; None for a bare image ID."""
    ref = (ref or "").strip()
    if not ref or ref.startswith("sha256:") or re.fullmatch(r"[0-9a-f]{12,64}", ref):
        return None
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", ref
    tag = None
    if ":" in path.rsplit("/", 1)[-1]:
        path, tag = path.rsplit(":", 1)
    if registry in ("index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"):
        registry = "docker.io"
    if registry == "docker.io" and "/" not in path:
        path = "library/" + path
    if not path:
        return None
    return ImageRef(registry, path, tag or (None if digest else "latest"), digest)


def local_digests(ref: ImageRef, repo_digests: list[str]) -> set[str]:
    """The digests this image was pulled as from ref's repository."""
    out = set()
    for entry in repo_digests:
        name, _, digest = entry.partition("@")
        other = parse_ref(name)
        if digest and other and (other.registry, other.repo) == (ref.registry, ref.repo):
            out.add(digest)
    return out


def looks_like_version_tag(tag: str | None) -> bool:
    return bool(tag) and bool(VERSION_TAG_RE.match(tag)) and any(c.isdigit() for c in tag)


def best_version_tag(tags: list[str]) -> str | None:
    """The most specific version-looking tag: 1.27.3 over 1.27 over 1."""
    picked = [t for t in tags if looks_like_version_tag(t)]
    return max(picked, key=lambda t: (t.count(".") + t.count("-"), len(t)), default=None)


class RegistryError(httpx.HTTPError):
    pass


class Registry:
    """Registry lookups, cached alongside the release data in Upstream."""

    def __init__(self, upstream: Upstream):
        self.up = upstream

    @property
    def http(self) -> httpx.AsyncClient:
        return self.up.http

    async def _token(self, challenge: str, ref: ImageRef) -> str | None:
        if not challenge.lower().startswith("bearer"):
            return None  # basic auth: needs credentials we don't have
        params = dict(CHALLENGE_RE.findall(challenge))
        realm = params.pop("realm", None)
        if not realm:
            return None
        params.setdefault("scope", f"repository:{ref.repo}:pull")
        key = ("regtoken", realm, params.get("service"), params["scope"])
        hit = self.up._cache.get(key)
        if hit and time.time() - hit[0] < hit[1][1]:
            return hit[1][0]
        r = await self.http.get(realm, params=params, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        token = data.get("token") or data.get("access_token")
        if token:
            # Tokens usually last 5 minutes; reuse them for a little less.
            self.up._cache[key] = (time.time(), (token, max(30, int(data.get("expires_in") or 300) - 30)))
        return token

    async def _request(self, method: str, url: str, ref: ImageRef, accept: str = ACCEPT) -> httpx.Response:
        headers = {"Accept": accept}
        r = await self.http.request(method, url, headers=headers, timeout=20)
        if r.status_code == 401:
            token = await self._token(r.headers.get("www-authenticate", ""), ref)
            if token:
                r = await self.http.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"}, timeout=20)
        if r.status_code in (401, 403):
            raise RegistryError(f"{ref.registry} needs a login for {ref.repo}")
        if r.status_code == 404:
            raise RegistryError(f"{ref.registry}/{ref.repo}:{ref.tag} not found")
        if r.status_code == 429:
            raise RegistryError(f"{ref.registry} rate limit reached")
        r.raise_for_status()
        return r

    async def digest(self, ref: ImageRef) -> str:
        """The digest ref's tag points at now."""
        key = ("image", ref.registry, ref.repo, ref.tag)
        hit, value = self.up._get(key, DIGEST_TTL)
        if hit:
            return value
        url = f"{ref.api}/manifests/{ref.tag}"
        r = await self._request("HEAD", url, ref)
        digest = r.headers.get("docker-content-digest")
        if not digest:
            # Not every registry sends the header on HEAD. The digest is the
            # hash of the manifest exactly as served.
            r = await self._request("GET", url, ref)
            digest = r.headers.get("docker-content-digest") or "sha256:" + hashlib.sha256(r.content).hexdigest()
        return self.up._put(key, digest)

    async def hub_tags(self, ref: ImageRef) -> dict[str, list[str]]:
        """Docker Hub: digest -> tag names, for the most recently pushed tags."""
        key = ("hubtags", ref.repo)
        hit, value = self.up._get(key, TAGS_TTL)
        if hit:
            return value
        ns, _, name = ref.repo.partition("/")
        r = await self.http.get(f"https://hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags",
                                params={"page_size": 100, "ordering": "last_updated"}, timeout=15)
        r.raise_for_status()
        out: dict[str, list[str]] = {}
        for t in r.json().get("results") or []:
            if t.get("digest") and t.get("name"):
                out.setdefault(t["digest"], []).append(t["name"])
        return self.up._put(key, out)

    async def label_version(self, ref: ImageRef, digest: str, os: str | None, arch: str | None) -> str | None:
        """org.opencontainers.image.version of the image at digest, for this platform."""
        key = ("imgver", ref.registry, ref.repo, digest, os, arch)
        hit = self.up._cache.get(key)  # a digest never changes, so no fresh refetch
        if hit:
            return hit[1]
        r = await self._request("GET", f"{ref.api}/manifests/{digest}", ref)
        doc = r.json()
        if doc.get("mediaType") in INDEX_TYPES or "manifests" in doc:
            entries = [m for m in doc.get("manifests") or []
                       if (m.get("platform") or {}).get("os") == (os or "linux")
                       and (m.get("platform") or {}).get("architecture") == (arch or "amd64")]
            if not entries:
                return self.up._put(key, None)
            r = await self._request("GET", f"{ref.api}/manifests/{entries[0]['digest']}", ref)
            doc = r.json()
        config = (doc.get("config") or {}).get("digest")
        if not config:
            return self.up._put(key, None)
        r = await self._request("GET", f"{ref.api}/blobs/{config}", ref, accept="*/*")
        labels = ((r.json().get("config") or {}).get("Labels")) or {}
        return self.up._put(key, labels.get("org.opencontainers.image.version"))


def short(digest: str | None) -> str:
    return (digest or "").removeprefix("sha256:")[:12]


async def _versions(reg: Registry, ref: ImageRef, local: set[str], remote: str, img: dict) -> tuple[str | None, str | None]:
    """(installed, available) version numbers, where we can tell them."""
    installed = available = None
    if ref.registry == "docker.io":
        tags = await reg.hub_tags(ref)
        available = best_version_tag(tags.get(remote, []))
        installed = best_version_tag([t for d in local for t in tags.get(d, [])])
    elif remote not in local:
        available = await reg.label_version(ref, remote, img.get("os"), img.get("arch"))
    installed = installed or img.get("version")
    if remote in local:
        available = installed
    return installed, available


async def compose_components(reg: Registry, info: dict) -> list[dict]:
    """A component per compose service; also sets info["compose_projects"]."""
    images = info.get("docker_images") or {}
    seen: set[tuple[str, str]] = set()
    services: list[dict] = []
    for ctr in info.get("docker_containers") or []:
        if not ctr["project"] or not ctr["service"] or (ctr["project"], ctr["service"]) in seen:
            continue  # standalone containers, and the extra replicas of a scaled service
        seen.add((ctr["project"], ctr["service"]))
        services.append(ctr)
    projects = sorted({s["project"] for s in services})
    remotes: dict[ImageRef, str | Exception] = {}
    comps = []
    for ctr in sorted(services, key=lambda s: (s["project"], s["service"])):
        img = images.get(ctr["image_id"]) or {}
        ref = parse_ref(ctr["image"])
        name = ctr["service"] if len(projects) == 1 else f"{ctr['project']}/{ctr['service']}"
        comp = {
            "name": name, "source": "compose", "project": ctr["project"], "service": ctr["service"],
            "image": ctr["image"], "container_state": ctr["state"],
            "installed": None, "target": None, "target_tag": None, "upstream": None,
            "pinned": False, "pending": False, "state": "unknown", "error": None,
            "repo": None, "forge": None, "repo_url": None, "releases_url": None, "release_url": None,
        }
        m = FORGE_URL_RE.match((img.get("source") or "").removesuffix(".git"))
        if m:
            comp["forge"], comp["repo"] = FORGE_HOSTS[m.group(1)], m.group(2)
            comp.update(repo_links(comp["forge"], comp["repo"], None))
        comps.append(comp)
        local = local_digests(ref, img.get("digests") or []) if ref else set()
        tag_version = ref.tag if ref and looks_like_version_tag(ref.tag) else None
        comp["installed"] = tag_version or img.get("version") or (ref and ref.tag) or short(ctr["image_id"])
        if not ref or not local:
            # No digest from a registry: built on the spot (build:) or loaded by hand.
            comp["state"], comp["error"] = "untracked", "built locally"
            continue
        if ref.digest:
            comp["state"], comp["pinned"] = "current", True
            continue
        if ref not in remotes:
            try:
                remotes[ref] = await reg.digest(ref)
            except (httpx.HTTPError, ValueError) as e:
                remotes[ref] = e
        remote = remotes[ref]
        if isinstance(remote, Exception):
            comp["error"] = str(remote) if isinstance(remote, RegistryError) else \
                error_text(remote) if isinstance(remote, httpx.HTTPError) else str(remote)
            continue
        if tag_version:
            installed = available = tag_version  # pinned to a version; only rebuilds of it come down
        else:
            try:
                installed, available = await _versions(reg, ref, local, remote, img)
            except (httpx.HTTPError, ValueError) as e:
                log.info("version lookup for %s failed: %s", ctr["image"], e)
                installed = available = None
        comp["installed"] = installed or comp["installed"]
        if remote in local:
            comp["state"], comp["target"] = "current", comp["installed"]
        else:
            comp["state"] = "outdated"
            if available and available != comp["installed"]:
                comp["target"] = comp["target_tag"] = available
            else:
                comp["target"] = f"{comp['installed']} (new build {short(remote)[:7]})"
        comp["remote_digest"] = remote
    info["compose_projects"] = [
        {"name": p, "working_dir": next(s["working_dir"] for s in services if s["project"] == p),
         "config_files": next(s["config_files"] for s in services if s["project"] == p),
         "outdated": any(c["state"] == "outdated" for c in comps if c["project"] == p)}
        for p in projects
    ]
    return comps
