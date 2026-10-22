"""Login sessions and credential checks.

Sessions are stateless signed cookies: "<user>.<expiry>.<hmac>". The HMAC key
mixes a random secret kept in the data dir with the configured password, so
changing the password signs everyone out. Basic auth keeps working for API
clients and scripts.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from .config import Config

COOKIE = "lxcwatch_session"
SESSION_DAYS = 30
MAX_FAILURES = 10
LOCKOUT_SECONDS = 15 * 60


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
