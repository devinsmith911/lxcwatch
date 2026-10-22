import os
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


@dataclass(frozen=True)
class Config:
    pve_host: str
    ssh_user: str
    ssh_port: int
    ssh_key: str | None
    node_addresses: dict[str, str]
    username: str
    password: str
    data_dir: str
    scan_interval_minutes: int
    refresh_packages: bool
    keep_snapshots: int
    job_concurrency: int
    github_token: str | None
    scripts_base: str
    website_url: str
    ntfy_url: str | None
    ntfy_token: str | None
    webhook_url: str | None
    notify_packages: str  # none | security | all
    public_url: str | None

    @property
    def sudo(self) -> bool:
        return self.ssh_user != "root"


def load() -> Config:
    pve_host = _env("PVE_HOST")
    password = _env("PASSWORD")
    missing = [n for n, v in (("LXCWATCH_PVE_HOST", pve_host), ("LXCWATCH_PASSWORD", password)) if not v]
    if missing:
        raise SystemExit(f"missing required environment variables: {', '.join(missing)}")
    notify_packages = (_env("NOTIFY_PACKAGES", "security") or "security").lower()
    if notify_packages not in ("none", "security", "all"):
        raise SystemExit("LXCWATCH_NOTIFY_PACKAGES must be none, security or all")
    return Config(
        pve_host=pve_host,
        ssh_user=_env("SSH_USER", "root"),
        ssh_port=int(_env("SSH_PORT", "22")),
        ssh_key=_env("SSH_KEY"),
        node_addresses=_node_addresses(_env("NODE_ADDRESSES")),
        username=_env("USERNAME", "admin"),
        password=password,
        data_dir=_env("DATA_DIR", "./data"),
        scan_interval_minutes=int(_env("SCAN_INTERVAL_MINUTES", "360")),
        refresh_packages=_bool("REFRESH_PACKAGES", True),
        keep_snapshots=int(_env("KEEP_SNAPSHOTS", "3")),
        job_concurrency=int(_env("JOB_CONCURRENCY", "2")),
        github_token=_env("GITHUB_TOKEN"),
        scripts_base=_env("SCRIPTS_BASE", "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"),
        website_url=_env("WEBSITE_URL", "https://community-scripts.org"),
        ntfy_url=_env("NTFY_URL"),
        ntfy_token=_env("NTFY_TOKEN"),
        webhook_url=_env("WEBHOOK_URL"),
        notify_packages=notify_packages,
        public_url=_env("PUBLIC_URL"),
    )
