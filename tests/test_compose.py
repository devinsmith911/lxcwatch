"""Apps run with docker compose: probe parsing, registry checks, the update job."""

import asyncio
import os
import shlex
import stat
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from lxcwatch.jobs import app_plan, compose_cmd, node_script
from lxcwatch.probe import PROBE_DOCKER, parse_probe, probe_script
from lxcwatch.registry import Registry, best_version_tag, compose_components, local_digests, parse_ref
from lxcwatch.upstream import Upstream

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
GHCR_OLD = "sha256:" + "c" * 64
GHCR_NEW = "sha256:" + "d" * 64


@pytest.mark.parametrize("ref,want", [
    ("nginx", ("docker.io", "library/nginx", "latest", None)),
    ("nginx:1.27", ("docker.io", "library/nginx", "1.27", None)),
    ("louislam/uptime-kuma:1", ("docker.io", "louislam/uptime-kuma", "1", None)),
    ("docker.io/library/redis:7", ("docker.io", "library/redis", "7", None)),
    ("index.docker.io/library/redis", ("docker.io", "library/redis", "latest", None)),
    ("ghcr.io/immich-app/immich-server:release", ("ghcr.io", "immich-app/immich-server", "release", None)),
    ("registry.lan:5000/team/app", ("registry.lan:5000", "team/app", "latest", None)),
    ("localhost/app:dev", ("localhost", "app", "dev", None)),
    ("postgres:16@" + OLD, ("docker.io", "library/postgres", "16", OLD)),
    ("redis@" + OLD, ("docker.io", "library/redis", None, OLD)),
])
def test_parse_ref(ref, want):
    r = parse_ref(ref)
    assert (r.registry, r.repo, r.tag, r.digest) == want


def test_parse_ref_rejects_image_ids():
    assert parse_ref("sha256:" + "a" * 64) is None
    assert parse_ref("4f5c1e2a9b3d") is None
    assert parse_ref("") is None


def test_local_digests_match_the_same_repo_only():
    ref = parse_ref("nginx:latest")
    assert local_digests(ref, [f"nginx@{OLD}", f"docker.io/library/nginx@{NEW}", f"ghcr.io/x/nginx@{GHCR_OLD}"]) == {OLD, NEW}


def test_best_version_tag():
    assert best_version_tag(["latest", "1", "1.27", "1.27.3", "stable", "mainline"]) == "1.27.3"
    assert best_version_tag(["5.2.1-ls301", "latest", "5.2.1"]) == "5.2.1-ls301"
    assert best_version_tag(["latest", "release"]) is None


PROBE_OUT = "\n".join([
    "os=Debian GNU/Linux 13 (trixie)",
    "docker=28.1.1",
    "dctr:immich\timmich-server\t/opt/immich\t/opt/immich/docker-compose.yml\tFalse\t/immich_server\tghcr.io/immich-app/immich-server:release\tsha256:img1\trunning",
    "dctr:immich\tredis\t/opt/immich\t/opt/immich/docker-compose.yml\tFalse\t/immich_redis\tredis:7.4\tsha256:img2\trunning",
    "dctr:immich\timmich-server\t/opt/immich\t/opt/immich/docker-compose.yml\tTrue\t/immich_run_1\tghcr.io/immich-app/immich-server:release\tsha256:img1\texited",
    "dctr:web\tnginx\t/srv/web\t/srv/web/compose.yaml,/srv/web/compose.override.yaml\tFalse\t/web-nginx-1\tnginx\tsha256:img3\trunning",
    "dctr:web\tapp\t/srv/web\t/srv/web/compose.yaml,/srv/web/compose.override.yaml\tFalse\t/web-app-1\tweb-app\tsha256:img4\trunning",
    "dctr:\t\t\t\t\t/portable\tbusybox\tsha256:img5\texited",
    f"dimg:sha256:img1\tghcr.io/immich-app/immich-server@{GHCR_OLD}\tv1.130.0\thttps://github.com/immich-app/immich\tlinux\tamd64",
    f"dimg:sha256:img2\tredis@{OLD}\t\t\tlinux\tamd64",
    f"dimg:sha256:img3\tnginx@{OLD}\t\t\tlinux\tamd64",
    "dimg:sha256:img4\t\t\t\tlinux\tamd64",
    "probe_ok=1",
])


def test_parse_probe_docker_lines():
    info = parse_probe(PROBE_OUT)
    assert info["docker"] == "28.1.1"
    names = [c["name"] for c in info["docker_containers"]]
    assert names == ["immich_server", "immich_redis", "web-nginx-1", "web-app-1", "portable"]  # no one-offs
    web = info["docker_containers"][2]
    assert web["config_files"] == ["/srv/web/compose.yaml", "/srv/web/compose.override.yaml"]
    assert info["docker_containers"][4]["project"] is None
    assert info["docker_images"]["sha256:img1"]["version"] == "v1.130.0"
    assert info["docker_images"]["sha256:img4"]["digests"] == []


def test_probe_docker_section():
    assert "__DOCKER__" not in probe_script(False) and "__T__" not in probe_script(False)
    assert '{{"\\t"}}' in probe_script(False)
    subprocess.run(["sh", "-n"], input=PROBE_DOCKER.encode(), check=True)


def fake_registry(calls: list) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url)))
        url = str(req.url)
        if req.url.host == "auth.docker.io" or url.startswith("https://ghcr.io/token"):
            return httpx.Response(200, json={"token": "t", "expires_in": 300})
        if req.url.host == "hub.docker.com":
            if "/namespaces/library/repositories/nginx/tags" in url:
                return httpx.Response(200, json={"results": [
                    {"name": "latest", "digest": NEW}, {"name": "1.27.4", "digest": NEW}, {"name": "1.27", "digest": NEW},
                    {"name": "1.27.3", "digest": OLD}]})
            return httpx.Response(200, json={"results": []})
        if not req.headers.get("authorization"):
            realm = "https://auth.docker.io/token" if req.url.host == "registry-1.docker.io" else "https://ghcr.io/token"
            return httpx.Response(401, headers={"www-authenticate": f'Bearer realm="{realm}",service="x"'})
        if url == "https://registry-1.docker.io/v2/library/nginx/manifests/latest":
            return httpx.Response(200, headers={"docker-content-digest": NEW})
        if url == "https://registry-1.docker.io/v2/library/redis/manifests/7.4":
            return httpx.Response(200, headers={"docker-content-digest": NEW})  # a rebuild of 7.4
        if url == "https://ghcr.io/v2/immich-app/immich-server/manifests/release":
            return httpx.Response(200, headers={"docker-content-digest": GHCR_NEW})
        if url == f"https://ghcr.io/v2/immich-app/immich-server/manifests/{GHCR_NEW}":
            return httpx.Response(200, json={"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
                {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]})
        if url == "https://ghcr.io/v2/immich-app/immich-server/manifests/sha256:amd":
            return httpx.Response(200, json={"config": {"digest": "sha256:cfg"}})
        if url == "https://ghcr.io/v2/immich-app/immich-server/blobs/sha256:cfg":
            return httpx.Response(200, json={"config": {"Labels": {"org.opencontainers.image.version": "v1.131.0"}}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def make_registry(calls: list) -> Registry:
    up = Upstream(SimpleNamespace())
    up.http = httpx.AsyncClient(transport=fake_registry(calls))
    return Registry(up)


def test_compose_components():
    calls: list = []
    info = parse_probe(PROBE_OUT)
    comps = asyncio.run(compose_components(make_registry(calls), info))
    by = {c["name"]: c for c in comps}
    assert list(by) == ["immich/immich-server", "immich/redis", "web/app", "web/nginx"]

    server = by["immich/immich-server"]
    assert server["state"] == "outdated" and server["installed"] == "v1.130.0" and server["target"] == "v1.131.0"
    assert server["repo"] == "immich-app/immich" and server["forge"] == "github" and server["release_url"] is None

    redis = by["immich/redis"]  # pinned to a version tag: a rebuild of it
    assert redis["state"] == "outdated" and redis["installed"] == "7.4" and redis["target"] == "7.4 (new build bbbbbbb)"

    nginx = by["web/nginx"]  # versions from Docker Hub's tag list
    assert nginx["state"] == "outdated" and nginx["installed"] == "1.27.3" and nginx["target"] == "1.27.4"

    app = by["web/app"]
    assert app["state"] == "untracked" and app["error"] == "built locally"

    assert info["compose_projects"] == [
        {"name": "immich", "working_dir": "/opt/immich", "config_files": ["/opt/immich/docker-compose.yml"], "outdated": True},
        {"name": "web", "working_dir": "/srv/web", "config_files": ["/srv/web/compose.yaml", "/srv/web/compose.override.yaml"], "outdated": True},
    ]
    # Digest checks are HEAD requests (free on Docker Hub); manifests are only fetched from other registries.
    assert not [c for c in calls if c[0] == "GET" and "registry-1.docker.io" in c[1]]


def test_compose_components_current_and_errors():
    calls: list = []
    info = parse_probe("\n".join([
        "dctr:kuma\tkuma\t/opt/kuma\t\tFalse\t/kuma\tnginx:latest\tsha256:i1\trunning",
        "dctr:kuma\tpriv\t/opt/kuma\t\tFalse\t/priv\tregistry.lan/me/priv:1\tsha256:i2\trunning",
        f"dimg:sha256:i1\tnginx@{NEW}\t\t\tlinux\tamd64",
        f"dimg:sha256:i2\tregistry.lan/me/priv@{OLD}\t\t\tlinux\tamd64",
    ]))
    reg = make_registry(calls)
    comps = asyncio.run(compose_components(reg, info))
    by = {c["name"]: c for c in comps}
    assert by["kuma"]["state"] == "current" and by["kuma"]["installed"] == "1.27.4"  # single project: plain service names
    assert by["priv"]["state"] == "unknown" and by["priv"]["error"]
    assert info["compose_projects"][0]["outdated"] is False


def test_app_plan():
    projects = [{"name": "a", "outdated": True}, {"name": "b", "outdated": False}]
    compose = {"source": "compose", "state": "outdated"}
    # Only images are outdated: no community-scripts run.
    assert app_plan({"script": "docker", "components": [{"state": "current"}, compose], "compose_projects": projects}) == (False, [projects[0]])
    # Both.
    assert app_plan({"script": "x", "components": [{"state": "outdated"}], "compose_projects": projects}) == (True, [projects[0]])
    # Nothing outdated: the script if there is one, else refresh every project.
    assert app_plan({"script": "x", "components": [], "compose_projects": [projects[1]]}) == (True, [])
    assert app_plan({"components": [], "compose_projects": [projects[1]]}) == (False, [projects[1]])
    assert app_plan({"components": []}) == (False, [])


def test_node_script_compose_only():
    s = node_script("j", 101, "app", None, None, run_script=False,
                    compose=[{"name": "immich", "working_dir": "/opt/immich", "config_files": []}])
    assert "env PHS_SILENT=1" not in s and "rc=0" in s and "update_project immich /opt/immich" in s
    subprocess.run(["bash", "-n"], input=s.encode(), check=True)
    both = node_script("j", 101, "app", None, None, compose=[{"name": "x", "working_dir": "/x", "config_files": []}])
    assert both.index("env PHS_SILENT=1") < both.index("update_project x")


FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >> "$LOG"
case "$*" in
  "compose version") exit 0 ;;
  "compose pull --help") echo "  --ignore-buildable  Ignore images that can be built" ;;
  *" pull "*|*" pull") [ -n "$FAIL_PULL" ] && exit 1; exit 0 ;;
  *" up -d") exit 0 ;;
  "ps -a"*) printf '%s\\n' "$PS_OUT" ;;
  "image prune -f") exit 0 ;;
esac
"""


def run_compose(tmp_path, ps_out: str, fail_pull: str = "", prune: bool = False):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    (bindir / "sleep").write_text("#!/bin/sh\n")  # don't wait for real
    (bindir / "sleep").chmod(0o755)
    proj = tmp_path / "my app"
    proj.mkdir(exist_ok=True)
    cmd = compose_cmd([{"name": "app", "working_dir": str(proj), "config_files": [str(proj / "compose.yaml")]},
                       {"name": "gone", "working_dir": str(tmp_path / "missing"), "config_files": []}], prune)
    log = tmp_path / "log"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "LOG": str(log), "PS_OUT": ps_out, "FAIL_PULL": fail_pull}
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, env=env, timeout=60)
    return r, log.read_text() if log.exists() else ""


def test_compose_job_runs_pull_and_up(tmp_path):
    r, log = run_compose(tmp_path, "app-web-1: Up 3 seconds (healthy)\napp-migrate-1: Exited (0) 2 seconds ago", prune=True)
    assert f"docker compose -p app -f {tmp_path}/my app/compose.yaml pull --ignore-buildable" in log
    assert f"docker compose -p app -f {tmp_path}/my app/compose.yaml up -d" in log
    assert "docker image prune -f" in log
    assert "app: running" in r.stdout
    assert "gone: its folder" in r.stdout and r.returncode == 1  # the other project still ran


def test_compose_job_reports_unhealthy(tmp_path):
    r, _ = run_compose(tmp_path, "app-web-1: Up 1 minute (unhealthy)\napp-db-1: Up 1 minute")
    assert "app: not healthy" in r.stdout and "app-web-1: Up 1 minute (unhealthy)" in r.stdout and r.returncode == 1


def test_compose_job_pull_failure_skips_up(tmp_path):
    r, log = run_compose(tmp_path, "", fail_pull="1")
    assert "pull failed, nothing was restarted" in r.stdout and " up -d" not in log and r.returncode == 1
