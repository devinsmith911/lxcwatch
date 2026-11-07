"""Tell someone when a scan finds something new to update."""

import hashlib
import json
import logging
from urllib.parse import urlparse

import httpx

from .config import Config
from .cluster import stored_self_update
from .db import DB

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_MAX = 1024  # Pushover's message limit, in characters
SLACK_MAX_LINES = 40  # keeps a Slack message readable (and well under its size limit)

# Settings that can come from the UI (kv `notify_settings`) or the environment.
# key: (Config attribute, environment variable, secret). An environment variable
# wins and locks the field in the UI. Secrets are never sent back to the browser.
FIELDS = {
    "ntfy_url": ("ntfy_url", "LXCWATCH_NTFY_URL", False),
    "ntfy_token": ("ntfy_token", "LXCWATCH_NTFY_TOKEN", True),
    "pushover_token": ("pushover_token", "LXCWATCH_PUSHOVER_TOKEN", True),
    "pushover_user": ("pushover_user", "LXCWATCH_PUSHOVER_USER", True),
    "slack_url": ("slack_webhook_url", "LXCWATCH_SLACK_WEBHOOK_URL", True),
    "webhook_url": ("webhook_url", "LXCWATCH_WEBHOOK_URL", False),
    "packages": ("notify_packages", "LXCWATCH_NOTIFY_PACKAGES", False),
    "public_url": ("public_url", "LXCWATCH_PUBLIC_URL", False),
}
DEFAULTS = {**{k: "" for k in FIELDS}, "packages": "security"}
PACKAGES = ("none", "security", "all")
LABELS = {"ntfy_url": "The ntfy topic URL", "slack_url": "The Slack webhook URL",
          "webhook_url": "The webhook URL", "public_url": "lxcwatch's address"}
URL_FIELDS = tuple(LABELS)


def _saved(db: DB) -> dict:
    saved = json.loads(db.get("notify_settings") or "{}")
    return {k: v for k, v in saved.items() if k in FIELDS}


def _locked(cfg: Config) -> dict[str, str]:
    """Fields set in the environment, with the variable that sets them."""
    return {k: env for k, (attr, env, _) in FIELDS.items() if getattr(cfg, attr) is not None}


def load_settings(cfg: Config, db: DB) -> dict:
    """The settings in effect: environment first, then what was saved in the UI."""
    saved = _saved(db)
    return {k: getattr(cfg, attr) if getattr(cfg, attr) is not None else saved.get(k, DEFAULTS[k])
            for k, (attr, _, _) in FIELDS.items()}


def public_settings(cfg: Config, db: DB) -> dict:
    """What the Notifications dialog gets: secrets only say whether they're set."""
    s = load_settings(cfg, db)
    return {
        "values": {k: "" if secret else s[k] for k, (_, _, secret) in FIELDS.items()},
        "secrets_set": {k: bool(s[k]) for k, (_, _, secret) in FIELDS.items() if secret},
        "locked": _locked(cfg),
    }


def merge_settings(cfg: Config, db: DB, data: dict) -> tuple[dict, dict]:
    """Apply changes from the UI. Returns (what to save, the resulting settings).

    A missing or null field keeps its current value, so secrets the browser never
    saw survive a save; an empty string clears it. Locked fields are ignored."""
    saved = {**DEFAULTS, **_saved(db)}
    locked = _locked(cfg)
    for key in FIELDS:
        value = data.get(key)
        if key in locked or value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        saved[key] = value.strip()
    saved["packages"] = (saved["packages"] or "security").lower()
    if saved["packages"] not in PACKAGES:
        raise ValueError("package notifications must be none, security or all")
    effective = {**saved, **{k: getattr(cfg, FIELDS[k][0]) for k in locked}}
    for key in URL_FIELDS:
        url = effective[key]
        if url and (urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc):
            raise ValueError(f"{LABELS[key]} must be an http:// or https:// address")
    if bool(effective["pushover_token"]) != bool(effective["pushover_user"]):
        raise ValueError("Pushover needs both the application token and the user key")
    return saved, effective


def save_settings(cfg: Config, db: DB, data: dict) -> None:
    saved, _ = merge_settings(cfg, db, data)
    db.set("notify_settings", json.dumps(saved))


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


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(title: str, lines: list[dict], public_url: str | None) -> dict:
    """An incoming-webhook message in Slack's mrkdwn."""
    out = [f"*{_slack_escape(title)}*"]
    for item in lines[:SLACK_MAX_LINES]:
        link = f" (<{item['url']}|release notes>)" if item["url"] else ""
        out.append(f"• {_slack_escape(item['text'])}{link}")
    if len(lines) > SLACK_MAX_LINES:
        out.append(f"…and {len(lines) - SLACK_MAX_LINES} more")
    if public_url:
        out.append(f"<{public_url}|Open lxcwatch>")
    return {"text": "\n".join(out), "unfurl_links": False, "unfurl_media": False}


def collect(cfg: Config, db: DB) -> tuple[list[dict], list[str]]:
    """Return (items, dedupe keys) for updates not yet notified."""
    packages = load_settings(cfg, db)["packages"]
    lines: list[dict] = []
    keys: list[str] = []
    # lxcwatch's own updates are announced whatever the package setting says.
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
        if packages == "none":
            continue
        pkgs = info.get("packages", [])
        if packages == "security":
            pkgs = [p for p in pkgs if p["security"]]
        if not pkgs:
            continue
        digest = hashlib.sha1("\n".join(sorted(f"{p['name']}={p['to']}" for p in pkgs)).encode()).hexdigest()[:16]
        key = f"{ct['vmid']}:pkg:{digest}"
        if not db.was_notified(key):
            kind = "security update" if packages == "security" else "package update"
            lines.append({"text": f"{label}: {len(pkgs)} {kind}{'s' if len(pkgs) != 1 else ''}", "url": None, "vmid": ct["vmid"]})
            keys.append(key)
    return lines, keys


async def send(cfg: Config, db: DB, http: httpx.AsyncClient) -> None:
    lines, keys = collect(cfg, db)
    if not lines:
        return
    title = f"{len(lines)} LXC update{'s' if len(lines) != 1 else ''} available"
    if await deliver(cfg, db, http, title, lines, tags="package"):
        for key in keys:
            db.mark_notified(key)


def _check(r: httpx.Response) -> None:
    # Redirects aren't followed (a POST that follows a 302 turns into a GET and
    # looks like success), so anything but a 2xx is a failure.
    if not r.is_success:
        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)


def _body(lines: list[dict]) -> str:
    return "\n".join(i["text"] + (f"\n  {i['url']}" if i["url"] else "") for i in lines)


async def _ntfy(s: dict, http: httpx.AsyncClient, title: str, lines: list[dict], tags: str) -> None:
    headers = {"Title": title, "Tags": tags}
    if s["ntfy_token"]:
        headers["Authorization"] = f"Bearer {s['ntfy_token']}"
    if s["public_url"]:
        headers["Click"] = s["public_url"]
    _check(await http.post(s["ntfy_url"], content=_body(lines).encode(), headers=headers, follow_redirects=False))


async def _pushover(s: dict, http: httpx.AsyncClient, title: str, lines: list[dict], tags: str) -> None:
    data = {"token": s["pushover_token"], "user": s["pushover_user"], "title": title, "message": pushover_message(lines)}
    if s["public_url"]:
        data.update(url=s["public_url"], url_title="Open lxcwatch")
    _check(await http.post(PUSHOVER_URL, data=data, follow_redirects=False))


async def _slack(s: dict, http: httpx.AsyncClient, title: str, lines: list[dict], tags: str) -> None:
    _check(await http.post(s["slack_url"], json=slack_payload(title, lines, s["public_url"] or None), follow_redirects=False))


async def _webhook(s: dict, http: httpx.AsyncClient, title: str, lines: list[dict], tags: str) -> None:
    payload = {"title": title, "message": _body(lines), "items": lines, "url": s["public_url"] or None}
    _check(await http.post(s["webhook_url"], json=payload, follow_redirects=False))


# name, the setting that turns it on, sender
CHANNELS = (("ntfy", "ntfy_url", _ntfy), ("Pushover", "pushover_token", _pushover),
            ("Slack", "slack_url", _slack), ("webhook", "webhook_url", _webhook))


def _reason(e: httpx.HTTPError) -> str:
    # Never the URL itself: Slack's (and many webhooks') carry the secret.
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.is_redirect:
            return f"HTTP {e.response.status_code}: redirected elsewhere, check the address"
        detail = e.response.text.strip()[:200]
        return f"HTTP {e.response.status_code}{f': {detail}' if detail else ''}"
    return f"{type(e).__name__}{f': {e}' if str(e) else ''}"


async def deliver_each(s: dict, http: httpx.AsyncClient, title: str, lines: list[dict], tags: str = "package") -> list[dict]:
    """Send to every configured channel: [{channel, ok, error}]."""
    results = []
    for name, key, sender in CHANNELS:
        if not s[key]:
            continue
        try:
            await sender(s, http, title, lines, tags)
            results.append({"channel": name, "ok": True, "error": None})
        except httpx.HTTPError as e:
            log.warning("%s notification failed: %s", name, _reason(e))
            results.append({"channel": name, "ok": False, "error": _reason(e)})
    return results


async def deliver(cfg: Config, db: DB, http: httpx.AsyncClient, title: str, lines: list[dict], tags: str = "package") -> bool:
    """Send to every configured channel; True if one took it (or none is configured)."""
    results = await deliver_each(load_settings(cfg, db), http, title, lines, tags)
    return not results or any(r["ok"] for r in results)


async def send_test(cfg: Config, db: DB, http: httpx.AsyncClient, data: dict) -> list[dict]:
    """Send a test message using the settings in the dialog, saved or not."""
    _, s = merge_settings(cfg, db, data)
    if not any(s[key] for _, key, _ in CHANNELS):
        raise ValueError("Set up at least one channel first")
    lines = [{"text": "This is a test from lxcwatch. Update notifications will arrive here.", "url": None, "vmid": None}]
    return await deliver_each(s, http, "lxcwatch test notification", lines, tags="test_tube")
