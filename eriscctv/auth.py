"""Password hashing, challenge/response proofs, web sessions and login throttling."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

ITERATIONS = 120_000


def derive(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))


def make_secret(password: str) -> dict:
    """Stored form of a password: PBKDF2 key + salt (the plain password is never kept)."""
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "iterations": ITERATIONS, "key": derive(password, salt).hex()}


def check_secret(record: dict, password: str) -> bool:
    try:
        key = derive(password, bytes.fromhex(record["salt"]), int(record["iterations"]))
        return hmac.compare_digest(key, bytes.fromhex(record["key"]))
    except (KeyError, ValueError, TypeError):
        return False


def proof(key: bytes, *parts: str) -> str:
    """HMAC-SHA256 over the joined parts; used so passwords never cross the network."""
    return hmac.new(key, "|".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a).encode(), str(b).encode())


class Sessions:
    """In-memory login sessions keyed by a random cookie token."""

    def __init__(self, ttl: int = 7 * 86400):
        self.ttl = ttl
        self._items: dict = {}

    def create(self, username: str, role: str = "admin") -> str:
        self._gc()
        token = secrets.token_urlsafe(32)
        self._items[token] = {"username": username, "role": role, "expires": time.time() + self.ttl,
                              "created": time.time()}
        return token

    def get(self, token: str):
        item = self._items.get(token or "")
        if not item:
            return None
        if item["expires"] < time.time():
            self._items.pop(token, None)
            return None
        item["expires"] = time.time() + self.ttl
        return item

    def drop(self, token: str) -> None:
        self._items.pop(token or "", None)

    def drop_user(self, username: str, keep: str = "") -> None:
        for t in [t for t, s in self._items.items() if s["username"] == username and t != keep]:
            self._items.pop(t, None)

    def _gc(self) -> None:
        now = time.time()
        for t in [t for t, s in self._items.items() if s["expires"] < now]:
            self._items.pop(t, None)


class Throttle:
    """Blocks an address for a while after too many failed logins."""

    def __init__(self, limit: int = 6, window: int = 600, block: int = 300):
        self.limit, self.window, self.block = limit, window, block
        self._fails: dict = {}
        self._blocked: dict = {}

    def blocked_for(self, key: str) -> int:
        until = self._blocked.get(key, 0)
        left = int(until - time.time())
        if left <= 0:
            self._blocked.pop(key, None)
            return 0
        return left

    def fail(self, key: str) -> None:
        now = time.time()
        items = [t for t in self._fails.get(key, []) if now - t < self.window] + [now]
        self._fails[key] = items
        if len(items) >= self.limit:
            self._blocked[key] = now + self.block
            self._fails.pop(key, None)

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
