"""Thin wrapper around the OpenSSH client.

The system ssh binary gives us ControlMaster multiplexing (one TCP connection
per node no matter how many containers we probe) and accept-new host key
pinning without any extra dependencies.
"""

import asyncio
import os
import shlex
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .config import Config


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class SSHError(RuntimeError):
    pass


class SSH:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = os.path.join(cfg.data_dir, "ssh")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        # Unix socket paths max out around 104 bytes and %C adds 40.
        self.control_dir = self.dir if len(self.dir) < 56 else tempfile.mkdtemp(prefix="lxcw-", dir="/tmp")

    def _args(self, host: str) -> list[str]:
        args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={os.path.join(self.dir, 'known_hosts')}",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={os.path.join(self.control_dir, '%C')}",
            "-o", "ControlPersist=120",
            "-p", str(self.cfg.ssh_port),
        ]
        if self.cfg.ssh_key:
            args += ["-i", self.cfg.ssh_key, "-o", "IdentitiesOnly=yes"]
        args.append(f"{self.cfg.ssh_user}@{host}")
        return args

    def wrap(self, command: str) -> str:
        """Run a node command as root, via sudo when we log in as someone else."""
        if not self.cfg.sudo:
            return command
        return f"sudo -n sh -c {shlex.quote(command)}"

    async def run(self, host: str, command: str, timeout: float = 120, input: str | None = None) -> Result:
        proc = await asyncio.create_subprocess_exec(
            *self._args(host), self.wrap(command),
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input.encode() if input is not None else None), timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise SSHError(f"timed out after {timeout:.0f}s on {host}")
        return Result(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))

    async def stream(self, host: str, command: str) -> AsyncIterator[bytes]:
        """Yield raw stdout chunks until the remote command exits."""
        proc = await asyncio.create_subprocess_exec(
            *self._args(host), self.wrap(command),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(4096):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
