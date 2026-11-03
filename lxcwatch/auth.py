"""Login sessions and credential checks.

Sessions are stateless signed cookies: "<user>.<expiry>.<hmac>". The HMAC key
mixes a random secret kept in the data dir with the configured password, so
changing the password signs everyone out. Basic auth keeps working for API
clients and scripts.

With Cloudflare Access configured, a request is also signed in by a valid
Access JWT whose email is on the allowlist. It's checked on every request, so
there is no lxcwatch session to outlive a revocation in Access.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque

import httpx
import jwt

from .config import Config

log = logging.getLogger("lxcwatch")

COOKIE = "lxcwatch_session"
SESSION_DAYS = 30
MAX_FAILURES = 10
LOCKOUT_SECONDS = 15 * 60
ACCESS_HEADER = "cf-access-jwt-assertion"
ACCESS_LOGOUT = "/cdn-cgi/access/logout"
ACCESS_KEYS_MAX_AGE = 3600
ACCESS_UNKNOWN_KEY_RETRY = 60  # tokens can name any key; don't let them drive fetches
ACCESS_LEEWAY = 60


class Auth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        path = os.path.join(cfg.data_dir, "session.key")
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
        with open(path, "rb") as f:
            secret = f.read()
        self.key = hashlib.sha256(secret + cfg.password.encode()).digest()
        self.failures: dict[str, deque[float]] = defaultdict(deque)
        self.access_keys: dict[str, object] = {}
        self.access_keys_checked = 0.0
        self.access_keys_lock = asyncio.Lock()

    def check_credentials(self, user: str, password: str) -> bool:
        ok_user = secrets.compare_digest(user.encode(), self.cfg.username.encode())
        ok_pass = secrets.compare_digest(password.encode(), self.cfg.password.encode())
        return ok_user and ok_pass

    def check_basic(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return self.check_credentials(user, password)

    def _sign(self, payload: str) -> str:
        return hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> tuple[str, int]:
        max_age = SESSION_DAYS * 86400
        payload = f"{self.cfg.username}.{int(time.time()) + max_age}"
        return f"{payload}.{self._sign(payload)}", max_age

    def check_session(self, token: str | None) -> bool:
        if not token or token.count(".") < 2:
            return False
        payload, _, sig = token.rpartition(".")
        user, _, expiry = payload.rpartition(".")
        if not hmac.compare_digest(sig, self._sign(payload)):
            return False
        return user == self.cfg.username and expiry.isdigit() and int(expiry) > time.time()

    # Cloudflare Access

    async def _fetch_access_jwks(self) -> dict:
        url = f"https://{self.cfg.cf_access_team}.cloudflareaccess.com/cdn-cgi/access/certs"
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(url)
            r.raise_for_status()
            return r.json()

    async def _access_key(self, kid: str | None):
        async with self.access_keys_lock:
            age = time.time() - self.access_keys_checked
            if age >= (ACCESS_KEYS_MAX_AGE if kid in self.access_keys else ACCESS_UNKNOWN_KEY_RETRY):
                self.access_keys_checked = time.time()
                try:
                    jwks = jwt.PyJWKSet.from_dict(await self._fetch_access_jwks())
                    self.access_keys = {k.key_id: k.key for k in jwks.keys}
                except (httpx.HTTPError, ValueError, jwt.PyJWTError) as e:
                    # Known keys stay in use, so a Cloudflare blip doesn't lock anyone out.
                    log.warning("could not fetch Cloudflare Access keys: %s", e)
            return self.access_keys.get(kid)

    async def check_access(self, token: str | None) -> str | None:
        """The signed-in email, or None."""
        if not token or not self.cfg.cf_access_team:
            return None
        try:
            key = await self._access_key(jwt.get_unverified_header(token).get("kid"))
            if not key:
                log.warning("Cloudflare Access token signed with an unknown key")
                return None
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.cfg.cf_access_aud,
                issuer=f"https://{self.cfg.cf_access_team}.cloudflareaccess.com",
                leeway=ACCESS_LEEWAY, options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.PyJWTError as e:
            log.warning("rejected Cloudflare Access token: %s", e)
            return None
        email = str(claims.get("email", "")).lower()
        if email not in self.cfg.cf_access_emails:  # service tokens have no email
            log.warning("Cloudflare Access identity %r is not in LXCWATCH_CF_ACCESS_EMAILS", email or claims.get("common_name"))
            return None
        return email

    # brute-force guard

    def locked_out(self, client: str) -> bool:
        attempts = self.failures[client]
        while attempts and attempts[0] < time.time() - LOCKOUT_SECONDS:
            attempts.popleft()
        return len(attempts) >= MAX_FAILURES

    def record_failure(self, client: str) -> None:
        self.failures[client].append(time.time())

    def reset(self, client: str) -> None:
        self.failures.pop(client, None)
