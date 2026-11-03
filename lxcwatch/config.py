import os
import re
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"LXCWATCH_{name}")
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _node_addresses(raw: str | None) -> dict[str, str]:
    # "pve1=10.0.0.11,pve2=10.0.0.12"
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            name, addr = part.split("=", 1)
            out[name.strip()] = addr.strip()
    return out


def _cf_team(raw: str | None) -> str | None:
    # "myteam", "myteam.cloudflareaccess.com" or the full https:// URL
    if not raw:
        return None
    team = raw.strip().lower().removeprefix("https://").rstrip("/").removesuffix(".cloudflareaccess.com")
    if not re.fullmatch(r"[a-z0-9-]+", team):
        raise SystemExit("LXCWATCH_CF_ACCESS_TEAM must be your team name, as in <team>.cloudflareaccess.com")
    return team


@dataclass(frozen=True)
class Config:
    pve_host: str
    ssh_user: str
    ssh_port: int
    ssh_key: str | None
    node_addresses: dict[str, str]
    ignore_nodes: frozenset[str]
    username: str
    password: str
    data_dir: str
    scan_interval_minutes: int
    scan_on_startup: str  # overdue | always | never
    refresh_packages: bool
    keep_snapshots: int
    job_concurrency: int
    github_token: str | None
    scripts_base: str
    website_url: str
    ntfy_url: str | None
    ntfy_token: str | None
    pushover_token: str | None
    releases_url: str | None  # lxcwatch's own latest release; None disables the check
    pushover_user: str | None
    webhook_url: str | None
    notify_packages: str  # none | security | all
    public_url: str | None
    cf_access_team: str | None  # team name, as in <team>.cloudflareaccess.com; None when Access is off
    cf_access_aud: str | None
    cf_access_emails: frozenset[str]  # lowercase; these sign in as the one account

    @property
    def sudo(self) -> bool:
        return self.ssh_user != "root"


def load() -> Config:
    pve_host = _env("PVE_HOST")
    password = _env("PASSWORD")
    missing = [n for n, v in (("LXCWATCH_PVE_HOST", pve_host), ("LXCWATCH_PASSWORD", password)) if not v]
    if missing:
        raise SystemExit(f"missing required environment variables: {', '.join(missing)}")
    scan_on_startup = (_env("SCAN_ON_STARTUP", "overdue") or "overdue").lower()
    if scan_on_startup not in ("overdue", "always", "never"):
        raise SystemExit("LXCWATCH_SCAN_ON_STARTUP must be overdue, always or never")
    pushover_token, pushover_user = _env("PUSHOVER_TOKEN"), _env("PUSHOVER_USER")
    if bool(pushover_token) != bool(pushover_user):
        raise SystemExit("Pushover needs both LXCWATCH_PUSHOVER_TOKEN and LXCWATCH_PUSHOVER_USER")
    notify_packages = (_env("NOTIFY_PACKAGES", "security") or "security").lower()
    if notify_packages not in ("none", "security", "all"):
        raise SystemExit("LXCWATCH_NOTIFY_PACKAGES must be none, security or all")
    cf_access_team, cf_access_aud, cf_access_emails = None, None, frozenset()
    if _bool("CF_ACCESS_ENABLED", False):
        cf_access_team = _cf_team(_env("CF_ACCESS_TEAM"))
        cf_access_aud = _env("CF_ACCESS_AUD")
        cf_access_emails = frozenset(e.strip().lower() for e in (_env("CF_ACCESS_EMAILS") or "").split(",") if e.strip())
        if not (cf_access_team and cf_access_aud and cf_access_emails):
            raise SystemExit("LXCWATCH_CF_ACCESS_ENABLED needs LXCWATCH_CF_ACCESS_TEAM, LXCWATCH_CF_ACCESS_AUD and LXCWATCH_CF_ACCESS_EMAILS")
    return Config(
        pve_host=pve_host,
        ssh_user=_env("SSH_USER", "root"),
        ssh_port=int(_env("SSH_PORT", "22")),
        ssh_key=_env("SSH_KEY"),
        node_addresses=_node_addresses(_env("NODE_ADDRESSES")),
        ignore_nodes=frozenset(n.strip() for n in (_env("IGNORE_NODES") or "").split(",") if n.strip()),
        username=_env("USERNAME", "admin"),
        password=password,
        data_dir=_env("DATA_DIR", "./data"),
        scan_interval_minutes=int(_env("SCAN_INTERVAL_MINUTES", "360")),
        scan_on_startup=scan_on_startup,
        refresh_packages=_bool("REFRESH_PACKAGES", True),
        keep_snapshots=int(_env("KEEP_SNAPSHOTS", "3")),
        job_concurrency=int(_env("JOB_CONCURRENCY", "2")),
        github_token=_env("GITHUB_TOKEN"),
        scripts_base=_env("SCRIPTS_BASE", "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"),
        website_url=_env("WEBSITE_URL", "https://community-scripts.org"),
        ntfy_url=_env("NTFY_URL"),
        ntfy_token=_env("NTFY_TOKEN"),
        pushover_token=pushover_token,
        releases_url=None if not _bool("UPDATE_CHECK", True) else _env(
            "RELEASES_URL", "https://api.github.com/repos/devinsmith911/lxcwatch/releases/latest"),
        pushover_user=pushover_user,
        webhook_url=_env("WEBHOOK_URL"),
        notify_packages=notify_packages,
        public_url=_env("PUBLIC_URL"),
        cf_access_team=cf_access_team,
        cf_access_aud=cf_access_aud,
        cf_access_emails=cf_access_emails,
    )
