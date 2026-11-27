"""Running commands in a guest: containers with pct, VMs with the QEMU guest agent.

Containers are easy: `pct exec <vmid> -- sh -c <script>` streams its output
and exits with the script's code. VMs go through the guest agent instead
(`qm guest exec`), which needs qemu-guest-agent running in the VM and the
Guest Agent option on. It doesn't stream: it waits for the command, then
prints JSON with the output and exit code. PVE ships Perl with a JSON module
on every node, so a Perl one-liner turns that back into plain output and an
exit code.

Updates take minutes and their log should be live, so jobs in VMs start the
script detached inside the VM, logging to a file there, and the node-side job
script prints that file's new lines every couple of seconds (`gx` below).
"""

import shlex

TYPES = ("lxc", "qemu")

# The JSON `qm guest exec` prints: out-data, err-data, exitcode (missing if the
# command outlived --timeout, when only a pid comes back).
_UNPACK = ("perl -MJSON::PP -0777 -ne 'my $r = decode_json($_); print $r->{\"out-data\"} // \"\"; "
           "print STDERR $r->{\"err-data\"} // \"\"; exit(defined $r->{exitcode} ? $r->{exitcode} : 124)'")
# Exit codes of our own: the agent couldn't be reached, or the command ran too long.
AGENT_DOWN = 125
TIMED_OUT = 124


def agent_exec(vmid: int, script: str, timeout: int = 60) -> str:
    """Node command (POSIX sh) running `script` in VM `vmid` through the guest agent."""
    q = shlex.quote
    return (f"out=$(qm guest exec {vmid} --timeout {timeout} -- sh -c {q(script)} 2>&1) "
            f"|| {{ printf '%s\\n' \"$out\" >&2; exit {AGENT_DOWN}; }}; "
            f"printf '%s' \"$out\" | {_UNPACK}")


def exec_cmd(kind: str, vmid: int, script: str, timeout: int = 60) -> str:
    """Node command that runs sh `script` in the guest and exits with its code."""
    if kind == "qemu":
        return agent_exec(vmid, script, timeout)
    return f"pct exec {vmid} -- sh -c {shlex.quote(script)}"


def tool(kind: str) -> str:
    """The node CLI for this guest type (snapshot, rollback, reboot, status...)."""
    return "qm" if kind == "qemu" else "pct"


def label(kind: str) -> str:
    return "VM" if kind == "qemu" else "CT"


# Bash functions for job scripts on the node, for VMs. `gx <vmid> <job> <script>`
# runs the script detached in the VM (so a dropped agent connection, or the
# agent restarting during a package upgrade, doesn't kill it), prints its log
# as it grows and returns its exit code. The log stays in the VM's /var/tmp.
GX = r"""
qga() {  # qga <vmid> <timeout> <sh script> [args...]: run in the VM, print its output
  local vmid=$1 t=$2 out; shift 2
  out=$(qm guest exec "$vmid" --timeout "$t" -- sh -c "$@" 2>&1) || { printf '%s\n' "$out" >&2; return __AGENT_DOWN__; }
  printf '%s' "$out" | __UNPACK__
}
gx() {
  local vmid=$1 log=/var/tmp/lxcwatch-$2.log n=1 fails=0 chunk rc
  if ! qga "$vmid" 30 'setsid sh -c '\''( sh -c "$1"; echo "__GX_RC__ $?" ) > "$2" 2>&1'\'' gx "$1" "$2" </dev/null >/dev/null 2>&1 &' gx "$3" "$log"; then
    echo '[lxcwatch] could not start the command in the VM (is qemu-guest-agent running?)'
    return __AGENT_DOWN__
  fi
  while :; do
    sleep 2
    # The first line is how many complete lines the log has; then the new ones.
    if ! chunk=$(qga "$vmid" 30 'f=$1; [ -f "$f" ] || exit 3; t=$(wc -l < "$f" | tr -d " "); echo "$t"; [ "$t" -ge "$2" ] && sed -n "$2,${t}p" "$f"; exit 0' gx "$log" "$n" 2>/dev/null); then
      fails=$((fails + 1))
      if [ $fails -ge 90 ]; then echo '[lxcwatch] lost contact with the VM for 3 minutes; the command may still be running in it'; return 1; fi
      continue
    fi
    fails=0
    t=${chunk%%$'\n'*}
    if [ "$chunk" != "$t" ]; then
      while IFS= read -r line; do
        case "$line" in
          "__GX_RC__ "*) rc=${line#__GX_RC__ }; qga "$vmid" 30 'rm -f "$1"' gx "$log" >/dev/null 2>&1; return "$rc" ;;
        esac
        printf '%s\n' "$line"
      done <<< "${chunk#*$'\n'}"
    fi
    [ "$t" -ge "$n" ] 2>/dev/null && n=$((t + 1))
  done
}
""".replace("__UNPACK__", _UNPACK).replace("__AGENT_DOWN__", str(AGENT_DOWN))


def run_line(kind: str, vmid: int, job_id: str, script: str) -> str:
    """A job-script line running sh `script` in the guest, with live output."""
    if kind == "qemu":
        return f"gx {vmid} {shlex.quote(job_id)} {shlex.quote(script)}"
    return f"pct exec {vmid} -- sh -c {shlex.quote(script)} </dev/null"
