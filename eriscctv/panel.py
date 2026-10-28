"""Login/session plumbing shared by the camera panel and the server panel."""
from __future__ import annotations

import asyncio
import logging
import os

from .auth import Sessions, Throttle
from .web import FileResponse, HttpError, HttpServer, json_response

log = logging.getLogger("panel")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CSRF_HEADER = "x-requested-with"


class PanelAuth:
    """Cookie sessions + login throttling + CSRF header check for one HttpServer.

    verify(username, password) -> role or None   (runs in a thread: PBKDF2 is slow on purpose)
    """

    def __init__(self, web: HttpServer, cookie: str, verify, app_name: str):
        self.web = web
        self.cookie = cookie
        self.verify = verify
        self.sessions = Sessions()
        self.throttle = Throttle()
        web.on_request = self._hook
        web.route("POST", "/api/login")(self._login)
        web.route("POST", "/api/logout")(self._logout)
        web.static("/static/", STATIC)
        index = os.path.join(STATIC, app_name, "index.html")
        web.route("GET", "/")(lambda req: FileResponse(index, cache=True))

    def _hook(self, req) -> None:
        token = req.cookies.get(self.cookie, "")
        req.session = self.sessions.get(token)
        req.session_token = token if req.session else ""
        if req.path.startswith("/api/") and req.method not in ("GET", "HEAD") and \
                req.headers.get(CSRF_HEADER) != "eris-cctv":
            raise HttpError(403, "Missing request header")

    async def _login(self, req):
        data = req.json()
        ip = req.remote
        wait = self.throttle.blocked_for(ip)
        if wait:
            raise HttpError(429, "Too many failed logins. Try again in %d seconds." % wait)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        loop = asyncio.get_running_loop()
        role = await loop.run_in_executor(None, self.verify, username, password)
        if not role:
            self.throttle.fail(ip)
            log.warning("failed panel login for %r from %s", username, ip)
            await asyncio.sleep(0.7)
            raise HttpError(401, "Wrong username or password")
        self.throttle.success(ip)
        token = self.sessions.create(username, role)
        resp = json_response({"ok": True, "username": username, "role": role})
        resp.set_cookie(self.cookie, token, max_age=self.sessions.ttl, secure=req.secure)
        log.info("panel login: %s from %s", username, ip)
        return resp

    def _logout(self, req):
        self.sessions.drop(req.session_token)
        resp = json_response({"ok": True})
        resp.set_cookie(self.cookie, "", max_age=0, secure=req.secure)
        return resp


def need_login(req) -> dict:
    if not req.session:
        raise HttpError(401, "Login required")
    return req.session


def need_admin(req) -> dict:
    s = need_login(req)
    if s.get("role") != "admin":
        raise HttpError(403, "Administrator only")
    return s


def as_int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def as_float(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))
