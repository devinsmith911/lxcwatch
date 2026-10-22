import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import config, notify
from .cluster import Cluster
from .db import DB
from .jobs import JobError, Jobs
from .ssh import SSH
from .upstream import FORGE_WEB, Upstream, compare_url, releases_between

logging.basicConfig(level=os.environ.get("LXCWATCH_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lxcwatch")

cfg = config.load()
db = DB(cfg.data_dir)
ssh = SSH(cfg)
upstream = Upstream(cfg)
cluster = Cluster(cfg, ssh, db, upstream)
jobs = Jobs(cfg, ssh, db, cluster)
STATIC = os.path.join(os.path.dirname(__file__), "static")


async def full_scan() -> None:
    await cluster.scan_all()
    await notify.send(cfg, db, upstream.http)


async def scheduler() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await full_scan()
        except Exception:
            log.exception("scheduled scan failed")
        if cfg.scan_interval_minutes <= 0:
            return
        await asyncio.sleep(cfg.scan_interval_minutes * 60)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.resume()
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    await upstream.close()


app = FastAPI(title="lxcwatch", lifespan=lifespan, docs_url=None, redoc_url=None)


def _authorized(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    ok_user = secrets.compare_digest(user.encode(), cfg.username.encode())
    ok_pass = secrets.compare_digest(password.encode(), cfg.password.encode())
    return ok_user and ok_pass


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    if not _authorized(request.headers.get("authorization", "")):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="lxcwatch"'})
    # Browsers resend cached basic-auth credentials on cross-site form posts;
    # a custom header can't be added without a CORS preflight we never allow.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "lxcwatch":
        return JSONResponse({"detail": "missing X-Requested-With header"}, status_code=403)
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


def _summary(ct: dict) -> dict:
    info = ct["info"] or {}
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
        },
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


@app.post("/api/update")
async def update(req: UpdateRequest):
    if req.kind not in ("app", "os"):
        raise HTTPException(400, "kind must be app or os")
    started, errors = {}, {}
    for vmid in req.vmids:
        try:
            started[vmid] = jobs.start(vmid, req.kind, req.snapshot)
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
