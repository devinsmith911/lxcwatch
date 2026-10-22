import os
import shutil
import subprocess

import pytest

from lxcwatch.jobs import MARK, clean_line, node_script
from lxcwatch.probe import parse_probe, probe_script
from lxcwatch.upstream import Check, build_components, parse_checks, strip_v

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
    assert "pct exec 101 -- env PHS_SILENT=1 TERM=dumb update </dev/null" in s
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
