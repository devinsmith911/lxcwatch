"""Tell someone when a scan finds something new to update."""

import hashlib
import json
import logging

import httpx

from .config import Config
from .cluster import stored_self_update
from .db import DB

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_MAX = 1024  # Pushover's message limit, in characters


def pushover_message(lines: list[dict]) -> str:
    """The notification text, cut to fit Pushover's limit."""
    out: list[str] = []
    for n, item in enumerate(lines):
        entry = item["text"] + (f"\n  {item['url']}" if item["url"] else "")
        rest = len(lines) - n - 1
        more = f"\n…and {rest} more" if rest else ""
        if len("\n".join(out + [entry])) + len(more) > PUSHOVER_MAX:
            return "\n".join(out) + f"\n…and {len(lines) - n} more"
        out.append(entry)
    return "\n".join(out)


def collect(cfg: Config, db: DB) -> tuple[list[dict], list[str]]:
    """Return (items, dedupe keys) for updates not yet notified."""
    lines: list[dict] = []
    keys: list[str] = []
    # lxcwatch's own updates are announced whatever NOTIFY_PACKAGES says.
    own = stored_self_update(db)
    if own and not db.was_notified(f"self:{own['version']}"):
        lines.append({"text": f"lxcwatch {own['version']} is available (you have {own['current']})",
                      "url": own.get("url"), "vmid": None})
        keys.append(f"self:{own['version']}")
    for ct in db.containers():
        info = ct["info"]
        if not info:
            continue
        label = f"{ct['vmid']} {ct['name']}"
        for comp in info.get("components", []):
            if comp["state"] != "outdated":
                continue
            key = f"{ct['vmid']}:app:{comp['name']}:{comp['target']}"
            if not db.was_notified(key):
                lines.append({"text": f"{label}: {comp['name']} {comp['installed']} → {comp['target']}",
                              "url": comp.get("release_url"), "vmid": ct["vmid"]})
                keys.append(key)
        if cfg.notify_packages == "none":
            continue
        pkgs = info.get("packages", [])
        if cfg.notify_packages == "security":
            pkgs = [p for p in pkgs if p["security"]]
        if not pkgs:
            continue
        digest = hashlib.sha1("\n".join(sorted(f"{p['name']}={p['to']}" for p in pkgs)).encode()).hexdigest()[:16]
        key = f"{ct['vmid']}:pkg:{digest}"
        if not db.was_notified(key):
            kind = "security update" if cfg.notify_packages == "security" else "package update"
            lines.append({"text": f"{label}: {len(pkgs)} {kind}{'s' if len(pkgs) != 1 else ''}", "url": None, "vmid": ct["vmid"]})
            keys.append(key)
    return lines, keys


async def send(cfg: Config, db: DB, http: httpx.AsyncClient) -> None:
    lines, keys = collect(cfg, db)
    if not lines:
        return
    title = f"{len(lines)} LXC update{'s' if len(lines) != 1 else ''} available"
    if await deliver(cfg, http, title, lines, tags="package"):
        for key in keys:
            db.mark_notified(key)


async def deliver(cfg: Config, http: httpx.AsyncClient, title: str, lines: list[dict], tags: str = "package") -> bool:
    """Send to every configured channel; True if one took it (or none is configured)."""
    body = "\n".join(i["text"] + (f"\n  {i['url']}" if i["url"] else "") for i in lines)
    delivered = not (cfg.ntfy_url or cfg.webhook_url or cfg.pushover_token)  # nothing configured: just record them
    if cfg.ntfy_url:
        headers = {"Title": title, "Tags": tags}
        if cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
        if cfg.public_url:
            headers["Click"] = cfg.public_url
        try:
            (await http.post(cfg.ntfy_url, content=body.encode(), headers=headers)).raise_for_status()
            delivered = True
        except httpx.HTTPError as e:
            log.warning("ntfy notification failed: %s", e)
    if cfg.pushover_token:
        data = {"token": cfg.pushover_token, "user": cfg.pushover_user, "title": title, "message": pushover_message(lines)}
        if cfg.public_url:
            data.update(url=cfg.public_url, url_title="Open lxcwatch")
        try:
            (await http.post(PUSHOVER_URL, data=data)).raise_for_status()
            delivered = True
        except httpx.HTTPError as e:
            log.warning("Pushover notification failed: %s", e)
    if cfg.webhook_url:
        try:
            r = await http.post(cfg.webhook_url, json={"title": title, "message": body, "items": lines, "url": cfg.public_url})
            r.raise_for_status()
            delivered = True
        except httpx.HTTPError as e:
            log.warning("webhook notification failed: %s", e)
    return delivered
