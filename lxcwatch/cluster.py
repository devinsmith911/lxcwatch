import asyncio
import json
import logging
import re
import shlex
import time

import httpx

from .config import Config
from .db import DB
from .probe import parse_probe, probe_script
from .release import debian_stable, release_info
from .ssh import SSH
from .upstream import Upstream, build_components, parse_checks

log = logging.getLogger(__name__)

SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
PER_NODE_PARALLEL = 4


class Cluster:
    def __init__(self, cfg: Config, ssh: SSH, db: DB, upstream: Upstream):
        self.cfg, self.ssh, self.db, self.upstream = cfg, ssh, db, upstream
        self.nodes: dict[str, str] = {}
        self.scan_lock = asyncio.Lock()
        self.scanning: set[int] = set()
        self.is_busy = lambda vmid: False  # set by Jobs
        self.last_error: str | None = None
        self.cluster_name: str | None = None
        self.node_info: list[dict] = []
        self.contact: dict = {"ok": None, "at": None, "error": None}

    def addr_override(self, node: str) -> str | None:
        return self.cfg.node_addresses.get(node)

    def addr(self, node: str) -> str:
        return self.cfg.node_addresses.get(node) or self.nodes.get(node) or node

    async def resolve(self, node: str) -> str:
        """Node address, discovering the cluster first if we haven't yet."""
        if node not in self.cfg.node_addresses and node not in self.nodes:
            try:
                await self.discover()
            except Exception as e:
                log.warning("discovery failed while resolving %s: %s", node, e)
        return self.addr(node)

    async def pvesh(self, path: str, host: str | None = None, timeout: float = 60):
        r = await self.ssh.run(host or self.cfg.pve_host, f"pvesh get {path} --output-format json", timeout=timeout)
        if not r.ok:
            raise RuntimeError(f"pvesh get {path} failed: {r.err.strip() or r.rc}")
        return json.loads(r.out)

    async def discover(self) -> list[dict]:
        try:
            status = await self.pvesh("/cluster/status")
        except Exception as e:
            self.contact = {"ok": False, "at": time.time(), "error": str(e)}
            raise
        self.contact = {"ok": True, "at": time.time(), "error": None}
        nodes, info = {}, []
        self.cluster_name = None
        for entry in status:
            if entry.get("type") == "cluster":
                self.cluster_name = entry.get("name")
            elif entry.get("type") == "node":
                nodes[entry["name"]] = entry.get("ip") or (self.cfg.pve_host if entry.get("local") else entry["name"])
                info.append({"name": entry["name"], "address": self.addr_override(entry["name"]) or nodes[entry["name"]],
                             "online": bool(entry.get("online", 1)), "local": bool(entry.get("local"))})
        self.nodes = nodes
        self.node_info = sorted(info, key=lambda n: n["name"])
        resources = await self.pvesh("/cluster/resources --type vm")
        cts = [r for r in resources if r.get("type") == "lxc" and not r.get("template")]
        for ct in cts:
            self.db.upsert_seen(int(ct["vmid"]), ct["node"], ct.get("name") or "", ct.get("status") or "", ct.get("tags") or "")
        self.db.forget_missing({int(ct["vmid"]) for ct in cts})
        return cts

    async def scan_all(self) -> None:
        async with self.scan_lock:
            started = time.time()
            try:
                cts = await self.discover()
            except Exception as e:
                self.last_error = f"discovery failed: {e}"
                log.exception("discovery failed")
                return
            self.last_error = None
            sems: dict[str, asyncio.Semaphore] = {}

            async def one(ct: dict) -> None:
                sem = sems.setdefault(ct["node"], asyncio.Semaphore(PER_NODE_PARALLEL))
                async with sem:
                    await self.scan_one(int(ct["vmid"]), rediscover=False)

            await asyncio.gather(*(one(ct) for ct in cts))
            self.db.set("last_scan", json.dumps({"at": time.time(), "took": round(time.time() - started, 1), "count": len(cts)}))

    async def scan_one(self, vmid: int, rediscover: bool = True) -> None:
        if rediscover:
            try:
                await self.discover()
            except Exception as e:
                self.db.save_scan(vmid, None, f"discovery failed: {e}")
                return
        row = self.db.container(vmid)
        if not row or vmid in self.scanning:
            return
        if row["status"] != "running":
            self.db.save_scan(vmid, None, None)
            return
        if self.is_busy(vmid):
            return
        self.scanning.add(vmid)
        try:
            script = probe_script(self.cfg.refresh_packages)
            r = await self.ssh.run(await self.resolve(row["node"]), f"pct exec {vmid} -- sh -c {shlex.quote(script)}", timeout=300)
            info = parse_probe(r.out)
            if not info["probe_ok"]:
                raise RuntimeError(r.err.strip() or f"probe exited with {r.rc}")
            await self._resolve(info)
            self.db.save_scan(vmid, info, None)
        except Exception as e:
            log.warning("scan of %s failed: %s", vmid, e)
            self.db.save_scan(vmid, None, str(e))
        finally:
            self.scanning.discard(vmid)

    async def _resolve(self, info: dict) -> None:
        checks = []
        info["upstream_errors"] = []
        if not info["script"] and info.get("script_app"):
            checks = await self._resolve_embedded(info)
        elif info["script"]:
            try:
                text = await self.upstream.ct_script(info["script_base"], info["script"])
                if text is None:
                    info["upstream_errors"].append(f"ct/{info['script']}.sh not found upstream")
                else:
                    checks = parse_checks(text)
            except httpx.HTTPError as e:
                info["upstream_errors"].append(f"fetching ct/{info['script']}.sh: {e}")
        latest: dict[tuple[str, str], str | None] = {}
        for c in checks:
            try:
                latest[(c.forge, c.repo)] = await self.upstream.latest(c.forge, c.repo, c.tag_prefix)
            except httpx.HTTPError as e:
                info["upstream_errors"].append(f"{c.repo}: {e}")
        info["components"] = build_components(checks, info["versions"], latest)
        info["release"] = release_info(info, await debian_stable(self.upstream.http, self.upstream._cache))
        info["script_state"] = await self.upstream.script_state(info["slug"]) if info["slug"] else None

    async def _resolve_embedded(self, info: dict) -> list:
        """/usr/bin/update is a full ct script: find it upstream by its APP name."""
        app = info["script_app"].strip().lower()
        candidates = list(dict.fromkeys([app.replace(" ", ""), app.replace(" ", "-")]))
        info["script_detected_from"] = "embedded"
        for name in candidates:
            try:
                text = await self.upstream.ct_script(None, name)
            except httpx.HTTPError as e:
                info["upstream_errors"].append(f"fetching ct/{name}.sh: {e}")
                break
            if text is not None:
                info["script"] = info["slug"] = name
                return parse_checks(text)
        # No upstream match: use the release checks from the embedded copy.
        info["script"] = candidates[0]
        info["upstream_errors"].append(
            f"no upstream ct script named {' or '.join(candidates)}; using the copy in /usr/bin/update")
        return parse_checks("\n".join(info.get("embedded", [])))

    # snapshots

    async def snapshots(self, vmid: int) -> list[dict]:
        row = self.db.container(vmid)
        if not row:
            raise KeyError(vmid)
        snaps = await self.pvesh(f"/nodes/{row['node']}/lxc/{vmid}/snapshot", host=await self.resolve(row["node"]))
        return sorted((s for s in snaps if s.get("name") != "current"), key=lambda s: s.get("snaptime") or 0, reverse=True)

    async def delete_snapshot(self, vmid: int, name: str) -> None:
        if not SNAPSHOT_NAME_RE.match(name):
            raise ValueError("invalid snapshot name")
        row = self.db.container(vmid)
        if not row:
            raise KeyError(vmid)
        r = await self.ssh.run(await self.resolve(row["node"]), f"pct delsnapshot {vmid} {name}", timeout=600)
        if not r.ok:
            raise RuntimeError(r.err.strip() or f"pct delsnapshot exited with {r.rc}")
