import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import time
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from . import config, notify
from .auth import COOKIE, Auth
from .batch import BatchError, Batches
from .cluster import Cluster
from .db import DB
from .jobs import CLEAR_FAILURES, JobError, Jobs
from .release import HOST_CHECK, container_checks, host_checks
from .ssh import SSH
from .upstream import FORGE_WEB, Upstream, compare_url, releases_between

logging.basicConfig(level=os.environ.get("LXCWATCH_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lxcwatch")

cfg = config.load()
db = DB(cfg.data_dir)
ssh = SSH(cfg)
upstream = Upstream(cfg)
auth = Auth(cfg)
cluster = Cluster(cfg, ssh, db, upstream)
jobs = Jobs(cfg, ssh, db, cluster)
batches = Batches(db, jobs)
STATIC = os.path.join(os.path.dirname(__file__), "static")


async def full_scan() -> None:
    await cluster.scan_all()
    await notify.send(cfg, db, upstream.http)


# Scheduled scans run one interval after the last scan (scheduled, manual or
# a failed attempt), so restarting lxcwatch doesn't trigger a scan by itself.
STARTED_AT = time.time()
last_attempt = 0.0


def last_scan_at() -> float:
    raw = db.get("last_scan")
    return json.loads(raw)["at"] if raw else 0.0


def next_scan_at() -> float | None:
    if cfg.scan_interval_minutes <= 0:
        return None
    base = max(last_scan_at(), last_attempt)
    if cfg.scan_on_startup == "never":
        base = max(base, STARTED_AT)
    return base + cfg.scan_interval_minutes * 60


async def scheduled_scan() -> None:
    global last_attempt
    last_attempt = time.time()
    try:
        await full_scan()
    except Exception:
        log.exception("scheduled scan failed")


async def scheduler() -> None:
    await asyncio.sleep(5)
    if cfg.scan_on_startup == "always" or (cfg.scan_on_startup == "overdue" and not last_scan_at()):
        await scheduled_scan()
    while True:
        due = next_scan_at()
        if due is None:
            return
        # Re-check every minute: a manual scan in the meantime moves the schedule.
        if time.time() >= due and not cluster.scan_lock.locked():
            await scheduled_scan()
        await asyncio.sleep(min(60, max(1, due - time.time())))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.resume()
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    await upstream.close()


app = FastAPI(title="lxcwatch", lifespan=lifespan, docs_url=None, redoc_url=None)


OPEN_PATHS = {"/healthz", "/manifest.webmanifest", "/login", "/api/login"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Every state-changing request needs this header: a cross-site form can't
    # add it, and fetch() can't without a CORS preflight we never allow.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "lxcwatch":
        return JSONResponse({"detail": "missing X-Requested-With header"}, status_code=403)
    if path in OPEN_PATHS or path in PUBLIC_FILES:
        return await call_next(request)
    if auth.check_session(request.cookies.get(COOKIE)) or auth.check_basic(request.headers.get("authorization", "")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "not signed in"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}" if target != "/" else "/login", status_code=303)


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/login")
async def login_page(request: Request):
    if auth.check_session(request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(os.path.join(STATIC, "login.html"), headers={"Cache-Control": "no-cache"})


@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    client = _client(request)
    if auth.locked_out(client):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    if not auth.check_credentials(req.username, req.password):
        auth.record_failure(client)
        log.warning("failed login for %r from %s", req.username, client)
        raise HTTPException(401, "Wrong username or password.")
    auth.reset(client)
    token, max_age = auth.issue()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=max_age, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", path="/")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# Icons are fetched by browsers and home-screen shortcuts without credentials.
PUBLIC_FILES = {
    "/favicon.ico": "image/x-icon",
    "/icon.svg": "image/svg+xml",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
    "/apple-touch-icon.png": "image/png",
}
MANIFEST = {
    "name": "lxcwatch",
    "short_name": "lxcwatch",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f3f5fb",
    "theme_color": "#4f46e5",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


def _public_file(path: str):
    async def handler():
        return FileResponse(os.path.join(STATIC, path.lstrip("/")), media_type=PUBLIC_FILES[path],
                            headers={"Cache-Control": "public, max-age=86400"})
    return handler


for _path in PUBLIC_FILES:
    app.add_api_route(_path, _public_file(_path), methods=["GET"], include_in_schema=False)


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


def last_success_at(info: dict, vmid: int) -> float:
    """Latest sign that a community-script run succeeded in this container."""
    times = [info.get("update_mtime") or 0]
    times += [t for path, t in info.get("vertimes", {}).items() if not path.endswith(".pending")]
    times += [j["finished_at"] or 0 for j in db.jobs(vmid) if j["kind"] == "app" and j["status"] == "success"]
    return max(times)


def _summary(ct: dict) -> dict:
    info = ct["info"] or {}
    ok_at = last_success_at(info, ct["vmid"]) if info.get("failures") else 0
    # A failure followed by a successful run is history, not a problem.
    failures = [{**f, "superseded": bool(f.get("at") and ok_at > f["at"])} for f in info.get("failures", [])]
    comps = info.get("components", [])
    pkgs = info.get("packages", [])
    last = db.jobs(ct["vmid"], limit=1)
    return {
        "vmid": ct["vmid"],
        "node": ct["node"],
        "name": ct["name"],
        "status": ct["status"],
        "tags": [t for t in (ct["tags"] or "").replace(",", ";").split(";") if t],
        "os": info.get("os"),
        "script": info.get("script"),
        "script_url": f"{upstream.script_base(info.get('script_base'))}/ct/{info['script']}.sh" if info.get("script") else None,
        "script_page": f"{cfg.website_url}/scripts/{info['slug']}" if info.get("slug") else None,
        "script_state": info.get("script_state"),
        "components": comps,
        "outdated": sum(1 for c in comps if c["state"] == "outdated"),
        "packages": pkgs,
        "pkg_count": len(pkgs),
        "pkg_security": sum(1 for p in pkgs if p["security"]),
        "pkgmgr": info.get("pkgmgr"),
        "reboot_required": info.get("reboot_required", False),
        "stale_procs": info.get("stale_procs", []),
        "release": info.get("release"),
        "failures": failures,
        "restart_needed": bool(info.get("reboot_required") or info.get("stale_procs")),
        "upstream_errors": info.get("upstream_errors", []),
        "error": ct["error"],
        "scanned_at": ct["scanned_at"],
        "scanning": ct["vmid"] in cluster.scanning,
        "job": jobs.active.get(ct["vmid"]),
        "last_job": last[0] if last else None,
    }


@app.get("/api/containers")
async def containers():
    last = db.get("last_scan")
    return {
        "containers": [_summary(ct) for ct in db.containers()],
        "scan": {
            "running": cluster.scan_lock.locked(),
            "last": json.loads(last) if last else None,
            "error": cluster.last_error,
            "interval_minutes": cfg.scan_interval_minutes,
            "next_at": next_scan_at(),
        },
        "batch": batches.current,
        "connection": {
            "host": cfg.pve_host,
            "user": cfg.ssh_user,
            "port": cfg.ssh_port,
            "cluster": cluster.cluster_name,
            "nodes": cluster.node_info,
            **cluster.contact,
        },
        "now": time.time(),
    }


@app.post("/api/scan", status_code=202)
async def scan():
    if not cluster.scan_lock.locked():
        jobs._spawn(full_scan())
    return {"started": True}


@app.post("/api/containers/{vmid}/scan")
async def scan_one(vmid: int):
    await cluster.scan_one(vmid)
    ct = db.container(vmid)
    if not ct:
        raise HTTPException(404, "container not found")
    return _summary(ct)


class UpdateRequest(BaseModel):
    vmids: list[int]
    kind: str  # app | os
    snapshot: bool = False
    restart: str = "auto"  # OS updates only: auto | always | never


class BatchRequest(BaseModel):
    vmids: list[int]
    kinds: list[str]  # app and/or os
    snapshot: bool = False
    restart: str = "auto"
    stop_on_failure: bool = False
    only: dict[int, list[str]] | None = None  # per-container kinds


@app.post("/api/batch")
async def start_batch(req: BatchRequest):
    if req.restart not in ("auto", "always", "never"):
        raise HTTPException(400, "restart must be auto, always or never")
    try:
        return batches.start(req.vmids, req.kinds, req.snapshot, req.restart, req.stop_on_failure, req.only)
    except BatchError as e:
        raise HTTPException(409, str(e))


@app.post("/api/batch/stop")
async def stop_batch():
    batches.stop()
    return batches.current


@app.post("/api/batch/dismiss")
async def dismiss_batch():
    batches.dismiss()
    return {"ok": True}


@app.post("/api/update")
async def update(req: UpdateRequest):
    if req.kind not in ("app", "os"):
        raise HTTPException(400, "kind must be app or os")
    started, errors = {}, {}
    for vmid in req.vmids:
        try:
            started[vmid] = jobs.start(vmid, req.kind, req.snapshot, restart=req.restart)
        except JobError as e:
            errors[vmid] = str(e)
    return {"jobs": started, "errors": errors}


@app.get("/api/containers/{vmid}/snapshots")
async def snapshots(vmid: int):
    try:
        return await cluster.snapshots(vmid)
    except KeyError:
        raise HTTPException(404, "container not found")
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/containers/{vmid}/clear-failures")
async def clear_failures(vmid: int):
    """Delete community-scripts failure flags in the container (logs are kept)."""
    ct = db.container(vmid)
    if not ct:
        raise HTTPException(404, "container not found")
    if ct["status"] != "running":
        raise HTTPException(409, "container is not running")
    if jobs.busy(vmid):
        raise HTTPException(409, "a job is running on this container")
    addr = await cluster.resolve(ct["node"])
    r = await ssh.run(addr, f"pct exec {vmid} -- sh -c {shlex.quote(CLEAR_FAILURES)}", timeout=60)
    if not r.ok:
        raise HTTPException(502, r.err.strip() or f"exit {r.rc}")
    await cluster.scan_one(vmid)
    return _summary(db.container(vmid))


@app.get("/api/containers/{vmid}/release-check")
async def release_check(vmid: int):
    """Pre-checks for a Debian release upgrade: container state plus the host's support."""
    ct = db.container(vmid)
    if not ct:
        raise HTTPException(404, "container not found")
    info = ct["info"] or {}
    rel = info.get("release")
    if not rel:
        raise HTTPException(409, "no release upgrade available for this container")
    checks = container_checks(info, rel)
    try:
        addr = await cluster.resolve(ct["node"])
        r = await ssh.run(addr, HOST_CHECK.replace("{vmid}", str(vmid)), timeout=30)
        checks += host_checks(r.out, rel["target"])
    except Exception as e:
        checks.append({"level": "warn", "text": f"Couldn't check the Proxmox host: {e}"})
    if ct["status"] != "running":
        checks.append({"level": "block", "text": f"The container is {ct['status']}; start it first."})
    return {"release": rel, "checks": checks, "blocked": any(c["level"] == "block" for c in checks)}


class ReleaseRequest(BaseModel):
    snapshot: bool = True


@app.post("/api/containers/{vmid}/release-upgrade")
async def release_upgrade(vmid: int, req: ReleaseRequest):
    try:
        return {"job": jobs.start(vmid, "release", req.snapshot)}
    except JobError as e:
        raise HTTPException(409, str(e))


@app.post("/api/containers/{vmid}/restart")
async def restart(vmid: int):
    try:
        return {"job": jobs.start(vmid, "restart", False)}
    except JobError as e:
        raise HTTPException(409, str(e))


@app.post("/api/containers/{vmid}/snapshots/{name}/rollback")
async def rollback(vmid: int, name: str):
    try:
        return {"job": jobs.start(vmid, "rollback", False, target=name)}
    except JobError as e:
        raise HTTPException(409, str(e))


@app.delete("/api/containers/{vmid}/snapshots/{name}")
async def delete_snapshot(vmid: int, name: str):
    if jobs.busy(vmid):
        raise HTTPException(409, "a job is running on this container")
    try:
        await cluster.delete_snapshot(vmid, name)
    except KeyError:
        raise HTTPException(404, "container not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"deleted": name}


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@app.get("/api/releases")
async def releases(forge: str, repo: str, installed: str | None = None, target: str | None = None):
    """Release notes between the installed and target versions."""
    if forge not in FORGE_WEB or not REPO_RE.match(repo):
        raise HTTPException(400, "bad forge or repo")
    try:
        rels = await upstream.releases(forge, repo)
    except Exception as e:
        raise HTTPException(502, f"could not fetch releases: {e}")
    out = releases_between(rels, installed, target)
    out["compare_url"] = (
        compare_url(forge, repo, out["installed_tag"], out["target_tag"])
        if out.get("installed_tag") and out.get("target_tag") and out["installed_tag"] != out["target_tag"] else None
    )
    return out


@app.get("/api/jobs")
async def job_list(vmid: int | None = None, limit: int = 50):
    return db.jobs(vmid, min(limit, 200))


@app.get("/api/jobs/{job_id}")
async def job(job_id: str):
    j = db.job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    j = db.job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    # Subscribe and snapshot the log with no await in between, so no line
    # is missed or sent twice.
    q = jobs.subscribe(job_id)
    backlog = db.job(job_id)

    def done_event() -> str:
        final = db.job(job_id)
        return f"event: done\ndata: {json.dumps({'status': final['status'], 'rc': final['rc']})}\n\n"

    async def events():
        try:
            for line in backlog["log"].splitlines():
                yield f"data: {json.dumps(line)}\n\n"
            if backlog["status"] not in ("queued", "running"):
                yield done_event()
                return
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if line is None:
                    yield done_event()
                    return
                yield f"data: {json.dumps(line)}\n\n"
        finally:
            jobs.unsubscribe(job_id, q)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
