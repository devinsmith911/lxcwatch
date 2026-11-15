"""Interactive shells in containers, for the dashboard's terminal.

The browser connects a WebSocket; here `ssh -tt <node> pct enter <vmid>` runs
on a local pseudo-terminal, so the remote shell is an ordinary root login and
window size changes reach it. Binary frames carry terminal bytes both ways.
Text frames are JSON: from the browser {"resize": [cols, rows]} (or a
keepalive), to it {"error": "..."} or {"exit": rc} just before closing.
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import termios
from urllib.parse import urlsplit

from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("lxcwatch")

READ_SIZE = 65536


def same_origin(origin: str | None, host: str | None, public_url: str | None) -> bool:
    """WebSockets skip CORS, so a page on another site could open one with our
    cookie. Only accept the dashboard's own origin."""
    if not origin:
        return False
    netloc = urlsplit(origin).netloc.lower()
    if host and netloc == host.lower():
        return True
    return bool(public_url) and netloc == urlsplit(public_url).netloc.lower()


def set_size(fd: int, cols: int, rows: int) -> None:
    cols, rows = max(2, min(int(cols), 1000)), max(2, min(int(rows), 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            await asyncio.sleep(0.01)  # the remote side is slow to read; don't block the loop
            continue
        data = data[n:]


async def bridge(ws: WebSocket, argv: list[str], cols: int, rows: int) -> int | None:
    """Run argv on a new pty and connect it to the (accepted) WebSocket.

    Returns ssh's exit code, or None if the browser went away first."""
    master, slave = pty.openpty()
    set_size(slave, cols, rows)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            env={**os.environ, "TERM": "xterm-256color"},
        )
    except Exception:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)

    loop = asyncio.get_running_loop()
    out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)

    def readable() -> None:
        try:
            chunk = os.read(master, READ_SIZE)
        except BlockingIOError:
            return
        except OSError:  # EIO: ssh exited and closed the other end
            chunk = b""
        if not chunk:
            loop.remove_reader(master)
            out.put_nowait(None)
            return
        out.put_nowait(chunk)
        if out.full():  # the browser is behind; stop reading until it catches up
            loop.remove_reader(master)
            paused.set()

    paused = asyncio.Event()

    async def to_browser() -> None:
        while (chunk := await out.get()) is not None:
            await ws.send_bytes(chunk)
            if paused.is_set() and out.qsize() < out.maxsize // 2:
                paused.clear()
                loop.add_reader(master, readable)

    async def from_browser() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                await _write_all(master, msg["bytes"])
            elif msg.get("text"):
                try:
                    ctl = json.loads(msg["text"])
                    if "resize" in ctl:
                        set_size(master, *ctl["resize"])
                        # ssh may have no controlling terminal to get SIGWINCH from.
                        with contextlib.suppress(ProcessLookupError):
                            proc.send_signal(signal.SIGWINCH)
                except (ValueError, TypeError, OSError):
                    pass

    loop.add_reader(master, readable)
    sender, receiver = asyncio.create_task(to_browser()), asyncio.create_task(from_browser())
    try:
        done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        browser_left = receiver in done
    finally:
        for t in (sender, receiver):
            t.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        loop.remove_reader(master)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()  # ssh closes the channel; the remote shell gets SIGHUP
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        os.close(master)
    if browser_left:
        return None
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await ws.send_text(json.dumps({"exit": proc.returncode}))
        await ws.close()
    return proc.returncode
