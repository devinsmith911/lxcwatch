"""VMs: commands through the QEMU guest agent, with a fake `qm` that runs them here."""

import os
import subprocess
import sys

import pytest

from lxcwatch.cluster import probe_error
from lxcwatch.guest import AGENT_DOWN, GX, agent_exec, exec_cmd, run_line
from lxcwatch.jobs import node_script
from lxcwatch.probe import parse_probe, probe_script

# `qm guest exec <vmid> --timeout <t> -- <argv...>` runs argv locally and prints
# the agent's JSON, like the real one. $AGENT=down makes it fail like a VM
# without the agent. Other qm subcommands are logged.
FAKE_QM = f"""#!{sys.executable}
import json, os, subprocess, sys
a = sys.argv[1:]
if a[:2] == ["guest", "exec"]:
    if os.environ.get("AGENT") == "down":
        print("QEMU guest agent is not running", file=sys.stderr); sys.exit(255)
    argv = a[a.index("--") + 1:]
    r = subprocess.run(argv, capture_output=True, text=True)
    print(json.dumps({{"exited": 1, "exitcode": r.returncode, "out-data": r.stdout, "err-data": r.stderr}}, indent=2))
else:
    with open(os.environ["QMLOG"], "a") as f:
        f.write(" ".join(a) + "\\n")
    if a[:1] == ["status"]:
        print("status: running")
"""


@pytest.fixture
def node(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "qm").write_text(FAKE_QM)
    (bindir / "setsid").write_text('#!/bin/sh\nexec "$@"\n')  # not on macOS
    for f in bindir.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "QMLOG": str(tmp_path / "qm.log")}

    def run(script: str, shell: str = "sh", **extra):
        return subprocess.run([shell, "-c", script], capture_output=True, text=True, env={**env, **extra}, timeout=60)
    return run


def test_agent_exec_unpacks_output_and_exit_code(node):
    r = node(agent_exec(101, "echo out; echo err >&2; exit 7"))
    assert (r.stdout, r.stderr.strip(), r.returncode) == ("out\n", "err", 7)


def test_agent_exec_agent_down(node):
    r = node(agent_exec(101, "echo hi"), AGENT="down")
    assert r.returncode == AGENT_DOWN and "not running" in r.stderr
    msg = probe_error("qemu", type("R", (), {"rc": r.returncode, "err": r.stderr})())
    assert "qemu-guest-agent" in msg and "Guest Agent" in msg


def test_exec_cmd_lxc_unchanged():
    assert exec_cmd("lxc", 101, "echo hi") == "pct exec 101 -- sh -c 'echo hi'"
    assert run_line("lxc", 101, "j", "echo hi") == "pct exec 101 -- sh -c 'echo hi' </dev/null"


def test_gx_streams_log_and_returns_exit_code(node, tmp_path, monkeypatch):
    # The "VM" log lives in /var/tmp; give this run its own job id.
    job = f"test-{os.getpid()}"
    r = node(GX + f"\ngx 101 {job} 'echo one; sleep 1; echo \"two  spaced\"; echo; exit 3'\necho \"rc=$?\"", shell="bash")
    assert r.stdout.splitlines() == ["one", "two  spaced", "", "rc=3"], r.stderr
    assert not os.path.exists(f"/var/tmp/lxcwatch-{job}.log")  # cleaned up


def test_gx_agent_down(node):
    r = node(GX + "\ngx 101 j 'echo hi'\necho \"rc=$?\"", shell="bash", AGENT="down")
    assert "is qemu-guest-agent running?" in r.stdout and f"rc={AGENT_DOWN}" in r.stdout


def test_vm_job_script(node, tmp_path):
    s = node_script(f"vmjob-{os.getpid()}", 201, "os", "lxcwatch-x", None, restart="auto", guest="qemu",
                    packages=["openssl"])
    assert "qm snapshot 201 lxcwatch-x" in s and "pct " not in s and "for VM 201" in s
    subprocess.run(["bash", "-n"], input=s.encode(), check=True)
    rb = node_script("j", 201, "rollback", None, "lxcwatch-x", guest="qemu")
    assert "qm rollback 201 lxcwatch-x" in rb and "qm start 201" in rb and "starting VM" in rb


def test_vm_job_runs(node, tmp_path):
    # A whole job: restart, which only talks to qm on the node.
    r = node(node_script("j", 201, "restart", None, None, guest="qemu"), shell="bash")
    assert "restarting VM" in r.stdout and "__LXCWATCH_EXIT__ 0" in r.stdout
    assert (tmp_path / "qm.log").read_text() == "reboot 201\n"


def test_probe_kernel_check():
    assert "__GUEST__" not in probe_script(False, "qemu")
    info = parse_probe("kernel=6.12.38+deb13-amd64\nkernel_installed=6.12.41+deb13-amd64\nprobe_ok=1\n")
    assert info["kernel"] == "6.12.38+deb13-amd64" and info["kernel_installed"] == "6.12.41+deb13-amd64"


def test_lxc_scripts_unchanged_by_guest_support():
    s = node_script("j", 101, "os", None, None, restart="auto")
    assert "pct exec 101 -- sh -c" in s and "qm " not in s and "gx " not in s and "for CT 101" in s
