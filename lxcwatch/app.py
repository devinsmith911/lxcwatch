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

from . import __version__, auto, config, notify
from .auth import ACCESS_HEADER, ACCESS_LOGOUT, COOKIE, Auth
from .batch import BatchError, Batches
from .cluster import Cluster, stored_self_update
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


async def full_scan(fresh: bool = False) -> None:
    await cluster.scan_all(fresh)
    await notify.send(cfg, db, upstream.http)


def summaries() -> list[dict]:
    policies = db.auto_policies()
    last = (autoupd.last_run() if autoupd else None) or {}
    return [_summary(ct, policies, last) for ct in db.containers()]


autoupd = auto.AutoUpdater(cfg, db, cluster, jobs, batches, upstream.http, summaries, lambda: full_scan(fresh=True))


async def auto_loop() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            await autoupd.tick()
        except Exception:
            log.exception("automatic update check failed")
        await asyncio.sleep(30)


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
    # Refresh lxcwatch's own update notice now rather than at the next scan.
    jobs._spawn(cluster.check_self_update())
    task = asyncio.create_task(scheduler())
    auto_task = asyncio.create_task(auto_loop())
    yield
    task.cancel()
    auto_task.cancel()
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
    # Access goes first so the page can show it even alongside a password session.
    request.state.access_email = await auth.check_access(request.headers.get(ACCESS_HEADER))
    if (request.state.access_email or auth.check_session(request.cookies.get(COOKIE))
            or auth.check_basic(request.headers.get("authorization", ""))):
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
    if auth.check_session(request.cookies.get(COOKIE)) or await auth.check_access(request.headers.get(ACCESS_HEADER)):
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
async def logout(request: Request):
    # Access would sign the browser straight back in, so end that session too.
    resp = JSONResponse({"ok": True, "redirect": ACCESS_LOGOUT if request.state.access_email else "/login"})
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


# The dashboard's own files. They need a sign-in like the page itself, and
# no-cache so an upgrade never pairs a new page with old scripts.
APP_FILES = {
    "/css/app.css": "text/css",
    "/js/core.js": "text/javascript",
    "/js/table.js": "text/javascript",
    "/js/update-all.js": "text/javascript",
    "/js/auto.js": "text/javascript",
    "/js/dialogs.js": "text/javascript",
    "/js/activity.js": "text/javascript",
    "/js/status.js": "text/javascript",
    "/js/details.js": "text/javascript",
    "/js/main.js": "text/javascript",
}


def _app_file(path: str):
    async def handler():
        return FileResponse(os.path.join(STATIC, path.lstrip("/")), media_type=APP_FILES[path],
                            headers={"Cache-Control": "no-cache"})
    return handler


for _path in APP_FILES:
    app.add_api_route(_path, _app_file(_path), methods=["GET"], include_in_schema=False)


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


def last_success_at(info: dict, vmid: int) -> float:
    """Latest sign that a community-script run succeeded in this container."""
    times = [info.get("update_mtime") or 0]
    times += [t for path, t in info.get("vertimes", {}).items() if not path.endswith(".pending")]
    times += [j["finished_at"] or 0 for j in db.jobs(vmid) if j["kind"] == "app" and j["status"] == "success"]
    return max(times)


def _summary(ct: dict, policies: dict[int, dict] | None = None, last_run: dict | None = None) -> dict:
    info = ct["info"] or {}
    pol = (policies if policies is not None else db.auto_policies()).get(ct["vmid"]) or {}
    if last_run is None:
        last_run = autoupd.last_run() or {}
    last_auto = next((s for s in (last_run or {}).get("steps", [])[::-1] if s["vmid"] == ct["vmid"]), None) \
        or next(({"status": "skipped", "note": s["reason"]} for s in (last_run or {}).get("skipped", []) if s["vmid"] == ct["vmid"]), None)
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
        "lxcwatch_host": bool(info.get("lxcwatch_host")),
        "auto": {
            "policy": pol.get("policy", "off"),
            "rollback": bool(pol.get("rollback")),
            "paused": autoupd.paused(ct["vmid"], pol) if pol.get("paused_at") else False,
            "paused_reason": pol.get("paused_reason"),
            "last": last_auto,
        },
        # Package lists that couldn't be refreshed: "up to date" may be stale.
        "pkg_stale": bool(info.get("pkg_refresh_failed") or info.get("pkg_refresh_errors")),
        "pkg_refresh_errors": info.get("pkg_refresh_errors", []),
        "pkg_warnings": info.get("pkg_warnings", []),
        "reboot_required": info.get("reboot_required", False),
        "stale_procs": info.get("stale_procs", []),
        "release": info.get("release"),
        "failures": failures,
        "restart_needed": bool(info.get("reboot_required") or info.get("stale_procs")),
        "upstream_errors": info.get("upstream_errors", []),
        "error": ct["error"],
        "scanned_at": ct["scanned_at"],
        "started_at": ct.get("started_at"),
        "scanning": ct["vmid"] in cluster.scanning,
        "job": jobs.active.get(ct["vmid"]),
        "last_job": last[0] if last else None,
    }


@app.get("/api/containers")
async def containers(request: Request):
    last = db.get("last_scan")
    return {
        "containers": summaries(),
        "auto": _auto_status(),
        "scan": {
            "running": cluster.scan_lock.locked(),
            "last": json.loads(last) if last else None,
            "error": cluster.last_error,
            "interval_minutes": cfg.scan_interval_minutes,
            "next_at": next_scan_at(),
        },
        "batch": batches.current,
        "self_update": stored_self_update(db),
        "version": __version__,
        "access_email": request.state.access_email,
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
        jobs._spawn(full_scan(fresh=True))  # manual: fetch fresh release data
    return {"started": True}


@app.post("/api/containers/{vmid}/scan")
async def scan_one(vmid: int):
    await cluster.scan_one(vmid, fresh=True)
    ct = db.container(vmid)
    if not ct:
        raise HTTPException(404, "container not found")
    return _summary(ct)


class UpdateRequest(BaseModel):
    vmids: list[int]
    kind: str  # app | os
    snapshot: bool = False
    restart: str = "auto"  # package updates only: auto | always | never


class BatchRequest(BaseModel):
    vmids: list[int]
    kinds: list[str]  # app and/or os
    snapshot: bool = False
    restart: str = "auto"
    stop_on_failure: bool = False
    only: dict[int, list[str]] | None = None  # per-container kinds


def _auto_status() -> dict:
    s = auto.load_settings(db)
    return {"enabled": s["enabled"], "next_at": auto.next_window(time.time(), s), "running": autoupd.running,
            "last_run": autoupd.last_run(), "settings": s}


class SettingsRequest(BaseModel):
    auto: dict


@app.get("/api/settings")
async def get_settings():
    return {"auto": auto.load_settings(db), "server_time": time.time(), "timezone": time.strftime("%Z"),
            "utc_offset": time.strftime("%z"), "status": _auto_status()}


@app.put("/api/settings")
async def put_settings(req: SettingsRequest):
    try:
        s = auto.validate_settings(req.auto)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.set("auto_settings", json.dumps(s))
    return await get_settings()


class AutoPolicyRequest(BaseModel):
    vmids: list[int]
    policy: str
    rollback: bool | None = None  # None keeps each container's current setting


@app.post("/api/auto/policy")
async def set_auto_policy(req: AutoPolicyRequest):
    if req.policy not in auto.POLICIES:
        raise HTTPException(400, f"policy must be one of {', '.join(auto.POLICIES)}")
    known = {c["vmid"] for c in db.containers()}
    current = db.auto_policies()
    for vmid in req.vmids:
        if vmid not in known:
            raise HTTPException(404, f"CT {vmid} not found")
        rollback = req.rollback if req.rollback is not None else bool((current.get(vmid) or {}).get("rollback"))
        db.set_auto_policy(vmid, req.policy, rollback)
    return {"ok": True}


@app.post("/api/containers/{vmid}/auto/resume")
async def resume_auto(vmid: int):
    if not db.container(vmid):
        raise HTTPException(404, "container not found")
    db.pause_auto(vmid, None)
    return {"ok": True}


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


class SnapshotRequest(BaseModel):
    name: str
    description: str | None = None


@app.post("/api/containers/{vmid}/snapshots")
async def take_snapshot(vmid: int, req: SnapshotRequest):
    """A manual snapshot, any time; never removed by the automatic cleanup."""
    try:
        return {"job": jobs.start(vmid, "snapshot", False, target=req.name.strip(),
                                  description=(req.description or "").strip() or None)}
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
