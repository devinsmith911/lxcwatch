import os
import shutil
import subprocess

import pytest

from lxcwatch.jobs import MARK, clean_line, node_script
from lxcwatch.probe import parse_probe, probe_script
from lxcwatch.upstream import Check, build_components, deb_upstream_version, package_components, parse_checks, strip_v

HERE = os.path.dirname(__file__)


def test_strip_v():
    assert strip_v("v1.2.3") == "1.2.3"
    assert strip_v("V2") == "2"
    assert strip_v("vault-1.0") == "vault-1.0"
    assert strip_v("1.0") == "1.0"
    assert strip_v(None) is None


def test_parse_checks_simple_and_pinned_vars():
    script = """
APP="Demo"
RELEASE="v3.3.5"
function update_script() {
  if check_for_gh_release "paperless" "paperless-ngx/paperless-ngx"; then
    :
  fi
  # check_for_gh_release "commented" "nope/nope"
  if check_for_gh_release "Immich" "immich-app/immich" "${RELEASE}" "tested"; then :; fi
  check_for_codeberg_release "librewolf" "librewolf/source" && echo yes
  if check_for_gh_release "$UNKNOWN" "x/y"; then :; fi
}
"""
    checks = parse_checks(script)
    assert checks == [
        Check("github", "paperless", "paperless-ngx/paperless-ngx", None, None),
        Check("github", "Immich", "immich-app/immich", "v3.3.5", None),
        Check("codeberg", "librewolf", "librewolf/source", None, None),
    ]
    assert checks[1].key == "immich"


def test_parse_checks_real_immich_script():
    with open(os.path.join(HERE, "immich_ct.sh")) as f:
        checks = {c.app: c for c in parse_checks(f.read())}
    assert checks["Immich"].repo == "immich-app/immich"
    assert checks["Immich"].pin == "v3.3.5"
    assert checks["VectorChord"].pin == "1.1.1"


def test_build_components_states():
    checks = [
        Check("github", "paperless", "paperless-ngx/paperless-ngx", None, None),
        Check("github", "Immich", "immich-app/immich", "v3.3.5", None),
        Check("github", "VectorChord", "tensorchord/VectorChord", "1.1.1", None),  # not installed
        Check("github", "Uptime Kuma", "louislam/uptime-kuma", None, None),
    ]
    versions = {
        "/root/.paperless": "2.14.0",
        "/root/.immich": "3.3.5",
        "/root/.uptimekuma": "v1.23.0",
        "/root/.uptimekuma.pending": "1.22.0",
        "/opt/Legacy_version.txt": "v0.9",
    }
    latest = {
        ("github", "paperless-ngx/paperless-ngx"): "v2.15.1",
        ("github", "immich-app/immich"): "v3.4.0",
        ("github", "louislam/uptime-kuma"): "1.23.0",
    }
    comps = {c["name"]: c for c in build_components(checks, versions, latest)}
    assert comps["paperless"]["state"] == "outdated"
    assert comps["paperless"]["target"] == "2.15.1"
    # Pinned: what `update` installs is the pin, not upstream latest.
    assert comps["Immich"]["state"] == "current"
    assert comps["Immich"]["upstream"] == "3.4.0"
    assert "VectorChord" not in comps
    assert comps["Uptime Kuma"]["state"] == "current"
    assert comps["Uptime Kuma"]["pending"] is True
    assert comps["Legacy"]["state"] == "untracked"
    assert comps["Legacy"]["installed"] == "0.9"


def test_parse_probe():
    out = """os=Debian GNU/Linux 13 (trixie)
os_id=debian
script=paperless-ngx
slug=paperless-ngx
script_base=https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main
ver:/root/.paperless=2.14.0
pkgmgr=apt
pkg:libssl3t64 3.5.1-1 3.5.1-1+deb13u1 1
pkg:newpkg - 1.0 0
reboot_required=1
probe_ok=1
"""
    info = parse_probe(out)
    assert info["script"] == "paperless-ngx"
    assert info["versions"] == {"/root/.paperless": "2.14.0"}
    assert info["packages"][0] == {"name": "libssl3t64", "from": "3.5.1-1", "to": "3.5.1-1+deb13u1", "security": True}
    assert info["packages"][1]["from"] is None
    assert info["reboot_required"] and info["probe_ok"]
    assert parse_probe("garbage")["probe_ok"] is False


def test_clean_line_strips_ansi_and_spinners():
    assert clean_line(b"\x1b[33m  \xe2\xa0\x8b working\r\x1b[K\x1b[32m\xe2\x9c\x94\xef\xb8\x8f done\x1b[m") == "✔️ done"
    assert clean_line(b"\r\r") == ""


def test_node_script_shapes():
    s = node_script("j1", 101, "app", "lxcwatch-20261022-000000", None)
    assert "pct snapshot 101 lxcwatch-20261022-000000" in s
    assert "pct exec 101 -- env PHS_SILENT=1 TERM=xterm-256color update </dev/null" in s
    assert f"echo '{MARK} 90'" in s
    s = node_script("j2", 102, "rollback", None, "lxcwatch-x")
    assert "pct rollback 102 lxcwatch-x" in s and "pct snapshot" not in s
    subprocess.run(["bash", "-n"], input=s.encode(), check=True)
    subprocess.run(["bash", "-n"], input=node_script("j3", 103, "os", None, None).encode(), check=True)


def test_probe_is_valid_sh():
    subprocess.run(["sh", "-n"], input=probe_script(True).encode(), check=True)


DOCKER = shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
FAKE_UPDATE = """#!/usr/bin/env bash
# Community-Scripts update entrypoint (generated - do not edit by hand).
export SCRIPT_SLUG="paperless-ngx"
export UPDATE_SCRIPT_NAME="paperless-ngx"
export COMMUNITY_SCRIPTS_URL="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"
"""
LEGACY_UPDATE = 'bash -c "$(curl -fsSL https://github.com/community-scripts/ProxmoxVE/raw/main/ct/vaultwarden.sh)"\n'


@pytest.mark.skipif(not DOCKER, reason="docker not available")
@pytest.mark.parametrize("image,update", [("debian:13", FAKE_UPDATE), ("alpine:3.22", LEGACY_UPDATE)])
def test_probe_in_real_container(image, update):
    setup = (
        f"cat > /usr/bin/update <<'EOF'\n{update}EOF\n"
        "printf '2.14.0\\n' > /root/.paperless; printf 'v1.2' > /root/.vaultwarden; "
        "printf 'x\\ny\\n' > /root/.multiline; echo 'export X=1' > /root/.bashrc; "
        "printf '0.9\\n' > /opt/Old_version.txt 2>/dev/null || { mkdir -p /opt && printf '0.9\\n' > /opt/Old_version.txt; }\n"
    )
    r = subprocess.run(
        ["docker", "run", "--rm", "-i", image, "sh", "-c", setup + probe_script(True)],
        capture_output=True, text=True, timeout=600,
    )
    info = parse_probe(r.stdout)
    assert info["probe_ok"], r.stderr
    assert info["versions"]["/root/.paperless"] == "2.14.0"
    assert info["versions"]["/root/.vaultwarden"] == "v1.2"
    assert info["versions"]["/opt/Old_version.txt"] == "0.9"
    assert "/root/.multiline" not in info["versions"] and "/root/.bashrc" not in info["versions"]
    if image.startswith("debian"):
        assert info["script"] == "paperless-ngx" and info["slug"] == "paperless-ngx"
        assert info["pkgmgr"] == "apt"
    else:
        assert info["script"] == "vaultwarden"
        assert info["script_base"] == "https://github.com/community-scripts/ProxmoxVE/raw/main"
        assert info["pkgmgr"] == "apk"
    assert info["os_id"] in ("debian", "alpine")


def test_release_links():
    from lxcwatch.upstream import compare_url, repo_links
    links = repo_links("github", "paperless-ngx/paperless-ngx", "v2.15.1")
    assert links == {
        "repo_url": "https://github.com/paperless-ngx/paperless-ngx",
        "releases_url": "https://github.com/paperless-ngx/paperless-ngx/releases",
        "release_url": "https://github.com/paperless-ngx/paperless-ngx/releases/tag/v2.15.1",
    }
    assert repo_links("gitlab", "a/b", "1.0")["release_url"] == "https://gitlab.com/a/b/-/releases/1.0"
    assert repo_links("codeberg", "a/b", None)["release_url"] is None
    assert compare_url("github", "a/b", "v1", "v2") == "https://github.com/a/b/compare/v1...v2"


def test_components_carry_release_links():
    checks = [Check("github", "Immich", "immich-app/immich", "v3.3.5", None)]
    comp = build_components(checks, {"/root/.immich": "3.3.0"}, {("github", "immich-app/immich"): "v3.4.0"})[0]
    # Pinned: the link points at the release `update` will install, not upstream latest.
    assert comp["release_url"].endswith("/releases/tag/v3.3.5")
    assert comp["upstream_url"].endswith("/releases/tag/v3.4.0")


def test_releases_between():
    from lxcwatch.upstream import releases_between
    rels = [{"tag": t} for t in ("v1.5.0", "v1.4.2", "v1.4.1", "v1.4.0", "v1.3.0")]
    out = releases_between(rels, "1.4.0", "1.4.2")  # pinned below latest
    assert [r["tag"] for r in out["releases"]] == ["v1.4.2", "v1.4.1"]
    assert out["complete"] and out["installed_tag"] == "v1.4.0" and out["target_tag"] == "v1.4.2"
    out = releases_between(rels, "0.9", "v1.5.0")  # installed older than the fetched window
    assert len(out["releases"]) == 5 and not out["complete"]
    assert releases_between(rels, "1.5.0", "1.4.2")["releases"] == []


def test_node_script_token_stays_off_command_lines():
    s = node_script("j4", 104, "app", None, None, token=True)
    assert "trap 'rm -f /var/log/lxcwatch/j4.token' EXIT" in s
    assert "< /var/log/lxcwatch/j4.token" in s
    assert "GITHUB_TOKEN=" not in s  # read from stdin inside the container, never an argument
    subprocess.run(["bash", "-n"], input=s.encode(), check=True)


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_token_reaches_update_via_stdin():
    from lxcwatch.jobs import APP_CMD_TOKEN
    fake_update = 'printf "#!/bin/sh\\necho token=\\$GITHUB_TOKEN silent=\\$PHS_SILENT term=\\$TERM\\n" > /usr/bin/update && chmod +x /usr/bin/update && '
    r = subprocess.run(["docker", "run", "--rm", "-i", "debian:13", "sh", "-c", fake_update + APP_CMD_TOKEN],
                       input="ghp_example123\n", capture_output=True, text=True, timeout=120)
    assert r.stdout.strip() == "token=ghp_example123 silent=1 term=xterm-256color", r.stderr


def test_restart_modes_in_node_script():
    auto = node_script("j5", 105, "os", None, None, restart="auto")
    assert "pct reboot 105" in auto and "deleted" in auto and "no restart needed" in auto
    assert "pct reboot" in node_script("j6", 105, "os", None, None, restart="always")
    assert "pct reboot" not in node_script("j7", 105, "os", None, None, restart="never")
    assert "pct reboot 106" in node_script("j8", 106, "restart", None, None)
    for script in (auto, node_script("j8", 106, "restart", None, None)):
        subprocess.run(["bash", "-n"], input=script.encode(), check=True)


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_stale_process_detection():
    from lxcwatch.probe import STALE_PROCS
    # Copy sleep to a lib path, run it, then delete the file: the process now
    # maps a deleted binary, exactly like a service after an upgrade.
    setup = "mkdir -p /usr/lib/x && cp /bin/sleep /usr/lib/x/oldsvc && (/usr/lib/x/oldsvc 30 &) && sleep 0.5 && rm /usr/lib/x/oldsvc && "
    r = subprocess.run(["docker", "run", "--rm", "debian:13", "sh", "-c", setup + "echo \"[$(" + STALE_PROCS + ")]\""],
                       capture_output=True, text=True, timeout=120)
    assert r.stdout.strip() == "[oldsvc ]", (r.stdout, r.stderr)
    clean = subprocess.run(["docker", "run", "--rm", "debian:13", "sh", "-c", "echo \"[$(" + STALE_PROCS + ")]\""],
                           capture_output=True, text=True, timeout=120)
    assert clean.stdout.strip() == "[]", clean.stdout


def test_marker_files_are_not_versions():
    comps = build_components([], {"/root/.hushlogin": "", "/root/.flag": "yes", "/root/.app": "2.1"}, {})
    assert [c["name"] for c in comps] == ["app"]


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_probe_skips_hushlogin_and_markers():
    setup = ("touch /root/.hushlogin /root/.cloud-locale-test.skip; printf 'enabled\\n' > /root/.someflag; "
             "printf 'abc123def\\n' > /root/.sha; printf '2025.9.1\\n' > /root/.cloudflared;\n")
    r = subprocess.run(["docker", "run", "--rm", "-i", "debian:13", "sh", "-c", setup + probe_script(False)],
                       capture_output=True, text=True, timeout=120)
    info = parse_probe(r.stdout)
    assert info["versions"] == {"/root/.sha": "abc123def", "/root/.cloudflared": "2025.9.1"}, r.stdout


def test_parse_failure_flags():
    info = parse_probe("failed:.install-error.failed=1 1792650000\nerrinfo:.install-error.failed:EXIT_CODE=1\n"
                       "errinfo:.install-error.failed:COMMAND=clear\nprobe_ok=1\n")
    assert info["failures"] == [{"file": ".install-error.failed", "code": 1, "at": 1792650000,
                                 "errinfo": ["EXIT_CODE=1", "COMMAND=clear"]}]
    # Never shown as an app
    assert build_components([], {"/root/.install-error.failed": "1"}, {}) == []


def test_successful_app_update_clears_failure_flags():
    s = node_script("j9", 109, "app", None, None)
    assert "rm -f /root/.install-*.failed" in s
    assert "rm -f /root/.install-*.failed" not in node_script("j9", 109, "os", None, None)


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_probe_reports_failure_flags():
    setup = ("printf '1\\n' > /root/.install-error.failed; "
             "printf 'EXIT_CODE=1\\nLINE=391\\nCOMMAND=clear\\n--- OUTPUT ---\\nboom\\n' > /root/.install-error.log.errinfo;\n")
    r = subprocess.run(["docker", "run", "--rm", "-i", "debian:13", "sh", "-c", setup + probe_script(False)],
                       capture_output=True, text=True, timeout=120)
    info = parse_probe(r.stdout)
    assert info["versions"] == {}, r.stdout
    [fail] = info["failures"]
    assert fail["file"] == ".install-error.failed" and fail["code"] == 1 and fail["at"] > 0
    assert fail["errinfo"] == ["EXIT_CODE=1", "LINE=391", "COMMAND=clear", "--- OUTPUT ---", "boom"]


def test_parse_success_signals():
    info = parse_probe("update_mtime=1792651000\nver:/root/.app=2.1\nvertime:/root/.app=1792652000\nprobe_ok=1\n")
    assert info["update_mtime"] == 1792651000
    assert info["vertimes"] == {"/root/.app": 1792652000}


def test_main_app_shown_without_version_file():
    checks = [Check("github", "seerr", "seerr-team/seerr", None, None),
              Check("github", "VectorChord", "tensorchord/VectorChord", "1.1.1", None)]
    comps = build_components(checks, {}, {("github", "seerr-team/seerr"): "v3.0.1"})
    # Main app kept as "version unknown"; optional extra still hidden.
    assert [c["name"] for c in comps] == ["seerr"]
    assert comps[0]["missing_version"] and comps[0]["state"] == "unknown" and comps[0]["target"] == "3.0.1"
    assert comps[0]["version_file"] == "/root/.seerr"


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_probe_detects_embedded_ct_script():
    with open(os.path.join(HERE, "seerr_update_embedded.sh")) as f:
        embedded = f.read()
    setup = f"cat > /usr/bin/update <<'LXCWATCH_EOF'\n{embedded}LXCWATCH_EOF\nprintf '3.0.1\\n' > /root/.seerr\n"
    r = subprocess.run(["docker", "run", "--rm", "-i", "debian:13", "sh", "-c", setup + probe_script(False)],
                       capture_output=True, text=True, timeout=120)
    info = parse_probe(r.stdout)
    assert info["script"] is None and info["script_app"] == "Seerr", r.stdout[:500]
    assert parse_checks("\n".join(info["embedded"])) == [Check("github", "seerr", "seerr-team/seerr", None, None)]
    assert info["versions"]["/root/.seerr"] == "3.0.1"


def test_resolve_embedded_prefers_upstream_then_falls_back():
    import asyncio
    from types import SimpleNamespace
    from lxcwatch.cluster import Cluster

    class FakeUpstream:
        def __init__(self, scripts): self.scripts = scripts
        async def ct_script(self, base, name): return self.scripts.get(name)

    embedded = ['  if check_for_gh_release "seerr" "seerr-team/old-fork"; then']
    cl = Cluster.__new__(Cluster)
    cl.upstream = FakeUpstream({"seerr": 'check_for_gh_release "seerr" "seerr-team/seerr"'})
    info = {"script": None, "script_app": "Seerr", "embedded": embedded, "upstream_errors": []}
    assert [c.repo for c in asyncio.run(cl._resolve_embedded(info))] == ["seerr-team/seerr"]
    assert info["script"] == "seerr" and info["slug"] == "seerr" and not info["upstream_errors"]

    cl.upstream = FakeUpstream({})
    info = {"script": None, "script_app": "Nginx Proxy Manager", "embedded": embedded, "upstream_errors": []}
    assert [c.repo for c in asyncio.run(cl._resolve_embedded(info))] == ["seerr-team/old-fork"]
    assert info["script"] == "nginxproxymanager" and info["upstream_errors"]


def test_release_info_and_container_checks():
    from lxcwatch.release import container_checks, release_info
    info = {"os_id": "debian", "os_version": "12", "os_codename": "bookworm", "disk_free_kb": 2 * 1024 * 1024,
            "held": ["nodejs"], "usr_unmerged": False, "apt_sources": [
                {"file": "/etc/apt/sources.list.d/debian.sources", "line": "http://deb.debian.org/debian bookworm bookworm-updates"},
                {"file": "/etc/apt/sources.list.d/docker.list", "line": "deb https://download.docker.com/linux/debian bookworm stable"}]}
    rel = release_info(info, 13)
    assert rel["from"] == "bookworm" and rel["to"] == "trixie" and rel["target"] == 13
    assert [s["file"] for s in rel["third_party"]] == ["/etc/apt/sources.list.d/docker.list"]
    levels = {c["text"].split()[0]: c["level"] for c in container_checks(info, rel)}
    assert "block" not in levels.values()
    texts = " ".join(c["text"] for c in container_checks(info, rel))
    assert "download.docker.com" in texts and "nodejs" in texts and "3 GB" in texts
    assert release_info({**info, "os_version": "13"}, 13) is None
    assert release_info({**info, "os_id": "ubuntu"}, 13) is None
    assert release_info({**info, "os_version": "11"}, 13)["to"] == "bookworm"  # one major at a time
    low = container_checks({**info, "disk_free_kb": 500 * 1024}, rel)
    assert any(c["level"] == "block" for c in low)


def test_host_checks():
    from lxcwatch.release import host_checks
    soft = "pveversion=pve-manager/8.4.1\npm:DEBIAN_MAXIMUM_RELEASE => 14\ncfg:features: nesting=1\n"
    assert [c["level"] for c in host_checks(soft, 13)] == ["ok"]
    assert host_checks(soft.replace("14", "12"), 13)[0]["level"] == "warn"
    old = "pveversion=pve-manager/7.4\npm:unsupported debian version\npm:version <= 12\ncfg:unprivileged: 1\n"
    checks = host_checks(old, 13)
    assert checks[0]["level"] == "block" and "nesting" in checks[1]["text"]


def test_release_job_script():
    s = node_script("j10", 110, "release", "lxcwatch-x", "bookworm:trixie")
    assert "pct snapshot 110 lxcwatch-x" in s and "FROM=bookworm" in s and "TO=trixie" in s and "pct reboot 110" in s
    subprocess.run(["bash", "-n"], input=s.encode(), check=True)
    from lxcwatch.release import upgrade_script
    with pytest.raises(ValueError):
        upgrade_script("bookworm", "trixie; rm -rf /")


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_probe_collects_debian_release_details():
    from lxcwatch.release import release_info
    r = subprocess.run(["docker", "run", "--rm", "-i", "debian:12", "sh", "-c", probe_script(False)],
                       capture_output=True, text=True, timeout=120)
    info = parse_probe(r.stdout)
    assert info["os_version"] == "12" and info["os_codename"] == "bookworm"
    assert info["disk_free_kb"] > 0 and not info["usr_unmerged"]
    assert any("deb.debian.org" in s["line"] and "bookworm" in s["line"] for s in info["apt_sources"]), info["apt_sources"]
    rel = release_info(info, 13)
    assert rel["to"] == "trixie" and rel["third_party"] == []


def test_manual_snapshot_job_script():
    s = node_script("j11", 111, "snapshot", None, "manual-20261026-0912", description="before config change; it's fine")
    assert "pct snapshot 111 manual-20261026-0912 --description 'before config change; it'\"'\"'s fine'" in s
    assert "pct exec" not in s and "pct reboot" not in s
    subprocess.run(["bash", "-n"], input=s.encode(), check=True)
    assert "--description 'manual snapshot (lxcwatch)'" in node_script("j12", 111, "snapshot", None, "manual-x")


def test_deb_upstream_version():
    assert deb_upstream_version("10.11.0+deb13") == "10.11.0"
    assert deb_upstream_version("5:28.5.1-1~debian.13~trixie") == "28.5.1"
    assert deb_upstream_version("2025.10.0") == "2025.10.0"
    assert deb_upstream_version("2.7.12-1") == "2.7.12"


def _pkg(name, version="1.0", origin="Vendor", homepage=None):
    return {"name": name, "version": version, "origin": origin, "homepage": homepage}


def test_package_components_pick_the_apps_package():
    pkgs = [_pkg("jellyfin", "10.11.0+deb13", "Jellyfin"), _pkg("jellyfin-server", "10.11.0+deb13", "Jellyfin"),
            _pkg("jellyfin-ffmpeg7", "7.1-1", "Jellyfin"), _pkg("nodejs", "22.1.0-1nodesource1", "Node Source")]
    pending = [{"name": "jellyfin", "from": "10.11.0+deb13", "to": "10.11.1+deb13", "security": False}]
    [c] = package_components(pkgs, ["jellyfin", "jellyfin", None], pending, [])
    assert c["name"] == "jellyfin" and c["source"] == "apt" and c["origin"] == "Jellyfin"
    assert c["state"] == "outdated" and c["installed"] == "10.11.0+deb13" and c["target"] == "10.11.1+deb13"
    assert (c["forge"], c["repo"]) == ("github", "jellyfin/jellyfin")
    assert (c["notes_installed"], c["notes_target"]) == ("10.11.0", "10.11.1")
    assert c["releases_url"] == "https://github.com/jellyfin/jellyfin/releases"
    # Shortest related name when nothing matches exactly; -server before that.
    [c] = package_components([_pkg("plexmediaserver"), _pkg("plex-tools-extra")], ["plex"], [], [])
    assert c["name"] == "plexmediaserver"
    [c] = package_components([_pkg("redis-tools", origin=None), _pkg("redis-server", origin=None)], ["redis"], [],
                             [], "debian", "trixie")
    assert c["name"] == "redis-server" and c["state"] == "current" and c["origin"] == "Debian"
    assert c["package_url"] == "https://packages.debian.org/trixie/redis-server" and c["repo"] is None


def test_package_components_homepage_and_fallbacks():
    [c] = package_components([_pkg("cloudflared", "2025.9.1", "cloudflared", "https://github.com/cloudflare/cloudflared")],
                             ["cloudflared"], [], [])
    assert c["repo"] == "cloudflare/cloudflared"
    # Already tracked from a version file: don't show it twice.
    tracked = [{"name": "caddy", "state": "current"}]
    assert package_components([_pkg("caddy")], ["caddy"], [], tracked) == []
    # A tracked app ignores unrelated vendor packages (Node.js from NodeSource)...
    assert package_components([_pkg("nodejs")], ["immich"], [], tracked) == []
    # ...but with nothing else to show, vendor packages are the best guess.
    comps = package_components([_pkg("libfoo1"), _pkg("nodejs"), _pkg("bash", origin=None)], [None], [], [])
    assert [c["name"] for c in comps] == ["nodejs"]


def test_parse_probe_app_packages():
    info = parse_probe("apppkg:cloudflared\t2025.9.1\tcloudflared\thttps://github.com/cloudflare/cloudflared\n"
                       "apppkg:bash\t5.2.37-2\t-\t\nprobe_ok=1\n")
    assert info["app_packages"] == [
        {"name": "cloudflared", "version": "2025.9.1", "origin": "cloudflared", "homepage": "https://github.com/cloudflare/cloudflared"},
        {"name": "bash", "version": "5.2.37-2", "origin": None, "homepage": None},
    ]


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_probe_reports_app_packages():
    # A fake vendor repo that "ships" base-files, and a script named after bash.
    setup = ("L=/var/lib/apt/lists; mkdir -p $L; printf 'Origin: Example\\n' > $L/repo.example.com_dists_stable_InRelease; "
             "printf 'Package: base-files\\nVersion: 99\\n\\nPackage: not-installed\\n' > $L/repo.example.com_dists_stable_main_binary-amd64_Packages; "
             "printf 'Origin: Debian\\n' > $L/mirror.local_debian_dists_trixie_InRelease; "
             "printf 'Package: dash\\n' > $L/mirror.local_debian_dists_trixie_main_binary-amd64_Packages; "
             "printf 'export UPDATE_SCRIPT_NAME=\"bash\"\\n' > /usr/bin/update;\n")
    r = subprocess.run(["docker", "run", "--rm", "-i", "debian:13", "sh", "-c", setup + probe_script(False)],
                       capture_output=True, text=True, timeout=120)
    info = parse_probe(r.stdout)
    got = {p["name"]: p["origin"] for p in info["app_packages"]}
    assert got == {"base-files": "Example", "bash": None}, r.stdout + r.stderr
    assert info["os_codename"] == "trixie"


CLOUDFLARE_SIG = (
    "W: An error occurred during the signature verification. The repository is not updated and the previous index files will be used. "
    "OpenPGP signature verification failed: https://pkg.cloudflare.com/cloudflared bookworm InRelease: Sub-process /usr/bin/sqv returned an error code (1)\n"
    "W: Failed to fetch https://pkg.cloudflare.com/cloudflared/dists/bookworm/InRelease  Sub-process /usr/bin/sqv returned an error code (1), "
    "error message is: Signing key on FBA8C0EE63617C5EED695C43254B391D8CACCBF8 is not bound\n"
    "W: Some index files failed to download. They have been ignored, or old ones used instead.\n"
)


def test_refresh_errors_from_apt_and_apk():
    info = parse_probe("".join(f"pkgwarn:{l}\n" for l in CLOUDFLARE_SIG.splitlines()) + "probe_ok=1\n")
    assert not info["pkg_refresh_failed"]  # apt-get update still exited 0
    assert info["pkg_refresh_errors"] == [{"repo": "https://pkg.cloudflare.com/cloudflared", "reason": "signature check failed"}]
    assert len(info["pkg_warnings"]) == 3
    info = parse_probe("pkg_refresh_failed=1\npkgwarn:W: Failed to fetch http://deb.example.org/debian/dists/trixie/InRelease  "
                       "Could not resolve 'deb.example.org'\n"
                       "pkgwarn:WARNING: fetching https://dl-cdn.alpinelinux.org/alpine/v3.22/main: network error\nprobe_ok=1\n")
    assert info["pkg_refresh_failed"]
    assert info["pkg_refresh_errors"] == [
        {"repo": "http://deb.example.org/debian", "reason": "DNS lookup failed"},
        {"repo": "https://dl-cdn.alpinelinux.org/alpine/v3.22/main", "reason": "couldn't connect"},
    ]


@pytest.mark.skipif(not DOCKER, reason="docker not available")
def test_probe_reports_unreachable_repo():
    setup = "echo 'deb [trusted=yes] http://127.0.0.1:9/debian trixie main' > /etc/apt/sources.list.d/broken.list\n"
    r = subprocess.run(["docker", "run", "--rm", "-i", "debian:13", "sh", "-c", setup + probe_script(True)],
                       capture_output=True, text=True, timeout=300)
    info = parse_probe(r.stdout)
    assert info["probe_ok"], r.stderr
    assert info["pkg_refresh_errors"] == [{"repo": "http://127.0.0.1:9/debian", "reason": "couldn't connect"}], info["pkg_warnings"]


def test_stored_self_update_ignores_the_running_version(tmp_path, monkeypatch):
    import json
    from lxcwatch import cluster
    from lxcwatch.db import DB
    db = DB(str(tmp_path / "t.db"))
    monkeypatch.setattr(cluster, "__version__", "1.2.0")
    db.set("self_update", json.dumps({"current": "1.1.1", "version": "1.2.0", "url": "u"}))
    assert cluster.stored_self_update(db) is None
    db.set("self_update", json.dumps({"current": "1.1.1", "version": "1.3.0", "url": "u"}))
    assert cluster.stored_self_update(db) == {"current": "1.2.0", "version": "1.3.0", "url": "u"}


def test_manual_scan_rechecks_lxcwatch_release_fresh(tmp_path):
    import asyncio
    from lxcwatch import cluster as cl
    from lxcwatch.db import DB
    from lxcwatch.upstream import FRESH_SINCE

    seen = []

    class FakeUpstream:
        async def lxcwatch_latest(self):
            seen.append(FRESH_SINCE.get())
            return None

    c = cl.Cluster.__new__(cl.Cluster)
    c.db, c.upstream = DB(str(tmp_path / "t.db")), FakeUpstream()

    async def no_scan():
        return None
    c._scan_all = no_scan
    asyncio.run(c.scan_all(fresh=True))
    asyncio.run(c.scan_all())
    assert seen[0] is not None and seen[1] is None
