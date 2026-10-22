"""What we run inside each container, and how we read its output.

The probe is plain POSIX sh so it works on Debian, Ubuntu and Alpine
containers alike. It only reads state; the optional package index refresh
(apt-get update / apk update) is the one thing it changes.
"""

PROBE = r"""
REFRESH=__REFRESH__
if [ -r /etc/os-release ]; then . /etc/os-release; fi
echo "os=${PRETTY_NAME:-unknown}"
echo "os_id=${ID:-}"
if [ -f /usr/bin/update ]; then
  n=$(sed -n 's/^export UPDATE_SCRIPT_NAME="\([^"]*\)"$/\1/p' /usr/bin/update | head -n1)
  s=$(sed -n 's/^export SCRIPT_SLUG="\([^"]*\)"$/\1/p' /usr/bin/update | head -n1)
  b=$(sed -n 's/^export COMMUNITY_SCRIPTS_URL="\([^"]*\)"$/\1/p' /usr/bin/update | head -n1)
  if [ -z "$n" ]; then
    u=$(grep -oE 'https?://[^" ]+/ct/[A-Za-z0-9._-]+\.sh' /usr/bin/update | head -n1)
    if [ -n "$u" ]; then n=$(basename "$u" .sh); b=${u%/ct/*}; fi
  fi
  echo "script=$n"
  echo "slug=$s"
  echo "script_base=$b"
fi
for f in /root/.* /opt/*_version.txt; do
  [ -f "$f" ] || continue
  case "${f##*/}" in
    .bash*|.profile|.viminfo|.lesshst|.wget-hsts|.selected_editor|.sudo_as_admin_successful|*_history|.gitconfig|.npmrc|.rnd) continue ;;
  esac
  [ "$(wc -c <"$f")" -le 128 ] || continue
  [ "$(grep -c . "$f")" -le 1 ] || continue
  v=$(head -n1 "$f" | tr -d '\r')
  case "$v" in *[[:space:]]*) continue ;; esac
  echo "ver:$f=$v"
done
if command -v apt-get >/dev/null 2>&1; then
  echo "pkgmgr=apt"
  if [ "$REFRESH" = 1 ]; then apt-get update -qq >/dev/null 2>&1 || echo "pkg_refresh_failed=1"; fi
  apt-get -s -o Debug::NoLocking=1 dist-upgrade 2>/dev/null | awk '/^Inst /{
    name=$2; old="-"; new=$3
    if ($3 ~ /^\[/) { old=$3; gsub(/\[|\]/, "", old); new=$4 }
    gsub(/[()]/, "", new)
    sec = ($0 ~ /[Ss]ecurity/) ? 1 : 0
    print "pkg:" name " " old " " new " " sec
  }'
elif command -v apk >/dev/null 2>&1; then
  echo "pkgmgr=apk"
  if [ "$REFRESH" = 1 ]; then apk update -q >/dev/null 2>&1 || echo "pkg_refresh_failed=1"; fi
  apk version -l '<' 2>/dev/null | awk 'NR>1 && NF>=3 { print "pkg:" $1 " - " $3 " 0" }'
else
  echo "pkgmgr=unknown"
fi
if [ -f /var/run/reboot-required ]; then echo "reboot_required=1"; fi
echo "probe_ok=1"
"""


def probe_script(refresh: bool) -> str:
    return PROBE.replace("__REFRESH__", "1" if refresh else "0")


def parse_probe(text: str) -> dict:
    info: dict = {
        "os": None,
        "os_id": None,
        "script": None,
        "slug": None,
        "script_base": None,
        "versions": {},  # file path -> raw version
        "pkgmgr": None,
        "packages": [],
        "pkg_refresh_failed": False,
        "reboot_required": False,
        "probe_ok": False,
    }
    for line in text.splitlines():
        if line.startswith("ver:"):
            path, _, value = line[4:].partition("=")
            info["versions"][path] = value
        elif line.startswith("pkg:"):
            parts = line[4:].split()
            if len(parts) >= 3:
                info["packages"].append({
                    "name": parts[0],
                    "from": None if parts[1] == "-" else parts[1],
                    "to": parts[2],
                    "security": len(parts) > 3 and parts[3] == "1",
                })
        elif "=" in line:
            key, _, value = line.partition("=")
            if key in ("pkg_refresh_failed", "reboot_required", "probe_ok"):
                info[key] = value == "1"
            elif key in info:
                info[key] = value or None
    return info
