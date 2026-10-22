"""Debian release upgrades (e.g. 12 bookworm -> 13 trixie), one major at a time.

Only official Debian sources (anything on *.debian.org, including mirrors
proxied through apt-cacher style URLs) are switched to the new codename.
Third-party repos are left alone and reported, since vendors often lag behind
a new Debian release.
"""

import re
import shlex
import time

import httpx

DEBIAN_CODENAMES = {
    10: "buster", 11: "bullseye", 12: "bookworm", 13: "trixie", 14: "forky", 15: "duke",
}
MIN_FREE_KB = 1024 * 1024        # block below 1 GB free
WARN_FREE_KB = 3 * 1024 * 1024   # warn below 3 GB free

# Inside the container, as POSIX sh. FROM/TO are filled in (validated codenames).
UPGRADE_SCRIPT = r"""
set -e
FROM=__FROM__
TO=__TO__
export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a
O="-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
. /etc/os-release
if [ "${VERSION_CODENAME:-}" != "$FROM" ]; then
  echo "[lxcwatch] container is on ${VERSION_CODENAME:-unknown}, expected $FROM; nothing changed"; exit 3
fi

echo "[lxcwatch] step 1/4: bringing $FROM fully up to date"
apt-get update
apt-get -y $O full-upgrade
if [ ! -L /bin ]; then
  echo "[lxcwatch] /usr is not merged yet; installing usrmerge (required by $TO)"
  apt-get -y $O install usrmerge
fi

echo "[lxcwatch] step 2/4: switching Debian sources $FROM -> $TO"
backup=/root/lxcwatch-apt-sources-$FROM-$(date +%Y%m%d-%H%M%S).tar
tar -cf "$backup" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true
echo "[lxcwatch] old sources saved to $backup"
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
  [ -f "$f" ] || continue
  sed -i -E "/debian\.org/ s/\b$FROM\b/$TO/g" "$f"
done
for f in /etc/apt/sources.list.d/*.sources; do
  [ -f "$f" ] || continue
  # deb822: rewrite Suites only in stanzas whose URIs are Debian's.
  awk -v from="$FROM" -v to="$TO" '
    function swap(l,   k, m, f, out) {
      m = split(l, f, /[ \t]+/); out = f[1]
      for (k = 2; k <= m; k++) {
        if (f[k] == from) f[k] = to
        else if (index(f[k], from "-") == 1) f[k] = to substr(f[k], length(from) + 1)
        out = out " " f[k]
      }
      return out
    }
    function flush() { for (i = 1; i <= n; i++) { l = buf[i]; if (deb && l ~ /^Suites:/) l = swap(l); print l } n = 0; deb = 0 }
    /^[[:space:]]*$/ { flush(); print; next }
    { buf[++n] = $0; if ($0 ~ /^URIs:/ && $0 ~ /debian\.org/) deb = 1 }
    END { flush() }
  ' "$f" > "$f.lxcwatch" && cat "$f.lxcwatch" > "$f" && rm -f "$f.lxcwatch"
done
grep -rhE "debian\.org" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null | grep -vE '^[[:space:]]*#' | sed 's/^/[lxcwatch]   /' || true

echo "[lxcwatch] step 3/4: upgrading to $TO (this takes a while)"
apt-get update
apt-get -y $O upgrade --without-new-pkgs
apt-get -y $O full-upgrade

echo "[lxcwatch] step 4/4: checking the result"
. /etc/os-release
if [ "${VERSION_CODENAME:-}" != "$TO" ]; then
  echo "[lxcwatch] still reports ${VERSION_CODENAME:-unknown} after the upgrade"; exit 4
fi
echo "[lxcwatch] now on $PRETTY_NAME"
"""


def upgrade_script(from_codename: str, to_codename: str) -> str:
    known = set(DEBIAN_CODENAMES.values())
    if from_codename not in known or to_codename not in known:
        raise ValueError("unknown Debian codename")
    return UPGRADE_SCRIPT.replace("__FROM__", from_codename).replace("__TO__", to_codename)


# Collected by the probe on Debian containers (see probe.py).
PROBE_DEBIAN = r"""
if [ "${ID:-}" = debian ]; then
  echo "os_version=${VERSION_ID:-}"
  echo "os_codename=${VERSION_CODENAME:-}"
  echo "disk_free_kb=$(df -Pk / | awk 'NR==2 {print $4}')"
  echo "held=$(apt-mark showhold 2>/dev/null | tr '\n' ' ')"
  [ -L /bin ] || echo "usr_unmerged=1"
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$f" ] && grep -E '^[[:space:]]*deb(-src)?[[:space:]]' "$f" | sed "s|^[[:space:]]*|aptsrc:$f:|"
  done
  for f in /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    awk -v f="$f" '
      function out() { if (u != "" && !off) print "aptsrc:" f ":" u " " s; u = ""; s = ""; off = 0 }
      /^URIs:/ { u = $2 } /^Suites:/ { $1 = ""; s = substr($0, 2) } /^Enabled:[[:space:]]*no/ { off = 1 }
      /^[[:space:]]*$/ { out() } END { out() }
    ' "$f"
  done
fi
"""


async def debian_stable(http: httpx.AsyncClient, cache: dict) -> int | None:
    """Current Debian stable major, from the mirror's Release file (cached a day)."""
    hit = cache.get("debian_stable")
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    try:
        r = await http.get("https://deb.debian.org/debian/dists/stable/Release", headers={"Range": "bytes=0-2047"}, timeout=10)
        m = re.search(r"^Version:\s*(\d+)", r.text, re.M)
        value = int(m.group(1)) if m else None
    except httpx.HTTPError:
        value = None
    if value:
        cache["debian_stable"] = (time.time(), value)
    return value


def is_debian_source(line: str) -> bool:
    return "debian.org" in line


def release_info(info: dict, stable: int | None) -> dict | None:
    """What release upgrade is available for a scanned Debian container."""
    if info.get("os_id") != "debian" or not stable:
        return None
    try:
        current = int(str(info.get("os_version") or "").split(".")[0])
    except ValueError:
        return None
    if current >= stable or current + 1 not in DEBIAN_CODENAMES:
        return None
    sources = info.get("apt_sources", [])
    return {
        "current": current,
        "from": info.get("os_codename") or DEBIAN_CODENAMES.get(current),
        "target": current + 1,
        "to": DEBIAN_CODENAMES[current + 1],
        "stable": stable,
        "third_party": [s for s in sources if not is_debian_source(s["line"])],
    }


def container_checks(info: dict, rel: dict) -> list[dict]:
    """Pre-checks from the last scan: [{level: ok|warn|block, text}]."""
    checks = []
    free = info.get("disk_free_kb")
    if free is None:
        checks.append({"level": "warn", "text": "Couldn't read free disk space."})
    elif free < MIN_FREE_KB:
        checks.append({"level": "block", "text": f"Only {free // 1024} MB free on /. A release upgrade needs at least 1 GB, ideally 3 GB."})
    elif free < WARN_FREE_KB:
        checks.append({"level": "warn", "text": f"{free // 1024} MB free on /. 3 GB or more is safer for a release upgrade."})
    else:
        checks.append({"level": "ok", "text": f"{free // 1024 // 1024} GB free on /."})
    if info.get("held"):
        checks.append({"level": "warn", "text": f"Held packages won't be upgraded: {', '.join(info['held'])}."})
    if info.get("usr_unmerged"):
        checks.append({"level": "warn", "text": "/usr isn't merged. The upgrade installs usrmerge first, which converts it."})
    if rel["third_party"]:
        names = sorted({s["line"].split()[1] if s["line"].startswith("deb") else s["line"].split()[0] for s in rel["third_party"]})
        checks.append({"level": "warn", "text": "Third-party repos stay on " + rel["from"] + " and aren't switched: "
                       + ", ".join(names) + ". Check that each vendor supports Debian " + str(rel["target"]) + "."})
    else:
        checks.append({"level": "ok", "text": "Only official Debian repos are configured."})
    if not any(is_debian_source(s["line"]) for s in info.get("apt_sources", [])):
        checks.append({"level": "block", "text": "No official Debian sources found to switch."})
    return checks


HOST_CHECK = (
    "f=/usr/share/perl5/PVE/LXC/Setup/Debian.pm; "
    "echo \"pveversion=$(pveversion 2>/dev/null)\"; "
    "grep -oE 'DEBIAN_MAXIMUM_RELEASE => [0-9]+|unsupported debian version|version <= [0-9]+' $f 2>/dev/null | sed 's/^/pm:/'; "
    "pct config {vmid} | grep -E '^(features|unprivileged):' | sed 's/^/cfg:/'"
)


def host_checks(output: str, target: int) -> list[dict]:
    """Interpret HOST_CHECK output for upgrading a container to Debian `target`."""
    checks = []
    pm = [l[3:] for l in output.splitlines() if l.startswith("pm:")]
    cfg = {l[4:].split(":", 1)[0]: l[4:].split(":", 1)[1].strip() for l in output.splitlines() if l.startswith("cfg:")}
    pve = next((l.split("=", 1)[1] for l in output.splitlines() if l.startswith("pveversion=")), "")
    soft = next((int(p.split()[-1]) for p in pm if p.startswith("DEBIAN_MAXIMUM_RELEASE")), None)
    hard = next((int(p.split()[-1]) for p in pm if p.startswith("version <=")), None)
    hard_die = any(p == "unsupported debian version" for p in pm)
    host = f" ({pve})" if pve else ""
    if hard_die and hard is not None and target > hard:
        checks.append({"level": "block", "text": f"This Proxmox host{host} refuses to start Debian {target} containers "
                       f"(supports up to {hard}). Upgrade Proxmox / pve-container on the host first."})
    elif soft is not None and target > soft:
        checks.append({"level": "warn", "text": f"This Proxmox host{host} has only been tested with Debian up to {soft}; "
                       "it will start the container but warn."})
    elif soft is not None or hard is not None:
        checks.append({"level": "ok", "text": f"This Proxmox host{host} supports Debian {target} containers."})
    else:
        checks.append({"level": "warn", "text": f"Couldn't tell which Debian versions this Proxmox host{host} supports."})
    if "nesting=1" not in cfg.get("features", ""):
        checks.append({"level": "warn", "text": "Nesting isn't enabled for this container. Newer systemd versions in Debian "
                       f"{target} can misbehave without it (Options → Features → nesting)."})
    return checks


def job_command(vmid: int, from_codename: str, to_codename: str) -> str:
    return f"pct exec {vmid} -- sh -c {shlex.quote(upgrade_script(from_codename, to_codename))}"
