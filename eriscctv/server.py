"""Viewing server: accepts camera agents, relays live video to logged-in browsers."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import signal
import struct
import time

from . import __version__, certs
from .auth import ITERATIONS, Throttle, check_secret, make_secret, proof, same
from .mp4 import KeyframeDetector
from .panel import PanelAuth, need_admin, need_login
from .util import (IS_WINDOWS, config_path, data_path, load_json, log_tail, new_id, remove_pid, save_json,
                   valid_name, write_pid)
from .web import HttpError, HttpServer, make_server_ssl
from .ws import WebSocketClosed

log = logging.getLogger("server")

DEFAULT_PORT = 8800
GRACE_SECONDS = 12
USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,40}$")


def default_config() -> dict:
    return {"server_id": new_id(8), "name": "Eris CCTV Server", "port": DEFAULT_PORT, "bind": "0.0.0.0",
            "public_address": "", "tls": {"mode": "auto", "cert": "", "key": ""},
            "camera_accounts": [], "users": [], "fake_salt_key": secrets.token_hex(16)}


TLS_MODES = ("auto", "files", "off")


def load_config(path: str = "") -> dict:
    cfg = load_json(path or config_path("server")) or default_config()
    for k, v in default_config().items():
        cfg.setdefault(k, v)
    tls = cfg["tls"] if isinstance(cfg.get("tls"), dict) else {}
    if tls.get("mode") not in TLS_MODES:
        # 1.0 had {"enabled": bool}: "off" there meant "no certificate yet", so it becomes automatic
        tls["mode"] = "files" if tls.get("enabled") and tls.get("cert") and tls.get("key") else "auto"
    tls.pop("enabled", None)
    tls.setdefault("cert", "")
    tls.setdefault("key", "")
    cfg["tls"] = tls
    return cfg


def valid_user(name: str) -> str:
    name = str(name or "").strip()
    if not USER_RE.match(name):
        raise HttpError(400, "Usernames may use letters, digits and . _ - @ (max 40)")
    return name


def check_password(pw: str) -> str:
    pw = str(pw or "")
    if len(pw) < 6:
        raise HttpError(400, "Passwords need at least 6 characters")
    return pw


class Viewer:
    """A browser watching one camera. Binary messages: 1 init, 2 fragment, 3 fragment starting with a
    keyframe; it starts at a keyframe and waits for the next one after a dropped fragment."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.primed = False
        self.need_key = True

    def _put(self, item, force=False):
        try:
            self.q.put_nowait(item)
        except asyncio.QueueFull:
            if force:
                while not self.q.empty():
                    self.q.get_nowait()
                self.q.put_nowait(item)

    def send_meta(self, meta: dict):
        if not meta.get("online"):
            self.primed = False
        self._put((0, json.dumps(meta)), force=True)

    def on_init(self, meta: dict, data: bytes):
        self._put((0, json.dumps(meta)), force=True)
        self._put((1, data), force=True)
        self.primed = True
        self.need_key = True

    def on_frag(self, data: bytes, key: bool = True):
        if not self.primed or (self.need_key and not key):
            return
        try:
            self.q.put_nowait((3 if key else 2, data))
            self.need_key = False
        except asyncio.QueueFull:
            self.need_key = True


class AgentConn:
    def __init__(self, sock, agent_id, name, account, ip, version, secure=False):
        self.sock = sock
        self.agent_id = agent_id
        self.name = name
        self.account = account
        self.ip = ip
        self.version = version
        self.secure = secure
        self.connected_at = time.time()
        self.cams: dict = {}      # slot -> camera info
        self.by_id: dict = {}     # camera id -> slot
        self.inits: dict = {}     # slot -> init segment
        self.keys: dict = {}      # slot -> KeyframeDetector for that init
        self.subscribed = set()   # slots the agent is streaming to us
        self.bytes_in = 0
        self._rate = (time.monotonic(), 0, 0)  # (time, bytes_in, bytes/s) of the last sample

    def rate_in(self) -> int:
        """Incoming bytes per second, sampled whenever the panel asks."""
        t0, b0, rate = self._rate
        now = time.monotonic()
        if now - t0 >= 1:
            rate = int((self.bytes_in - b0) / (now - t0))
            self._rate = (now, self.bytes_in, rate)
        return rate


class Server:
    def __init__(self, path: str = ""):
        self.path = path or config_path("server")
        self.cfg = load_config(self.path)
        self.state_path = data_path("server-agents.json")
        self.known: dict = load_json(self.state_path, {}) or {}
        self.agents: dict = {}    # agent_id -> AgentConn
        self.viewers: dict = {}   # "agent/cam" -> set(Viewer)
        self._unsub: dict = {}
        self.web = HttpServer("eris-cctv-server")
        self.agent_throttle = Throttle(limit=10, window=600, block=600)
        self.started = time.time()
        self.tls_cert = ""        # certificate in use ("" = camera connections cannot use TLS)
        self._save_pending = False
        self._stop = None
        self.auth = PanelAuth(self.web, "ecctv_srv", self._verify, "server")
        self._routes()

    # -- config helpers --------------------------------------------------------------------
    def save(self) -> None:
        save_json(self.path, self.cfg)

    def _schedule_state_save(self) -> None:
        if self._save_pending:
            return
        self._save_pending = True

        async def later():
            await asyncio.sleep(3)
            self._save_pending = False
            try:
                save_json(self.state_path, self.known)
            except OSError:
                log.exception("cannot save agent list")
        asyncio.ensure_future(later())

    def _verify(self, username: str, password: str):
        for u in self.cfg["users"]:
            if u["username"] == username and check_secret(u["secret"], password):
                return u.get("role", "viewer")
        return None

    def _user(self, username: str):
        for u in self.cfg["users"]:
            if u["username"] == username:
                return u
        return None

    def _account(self, username: str):
        for a in self.cfg["camera_accounts"]:
            if a["username"] == username:
                return a
        return None

    def _allowed(self, session: dict, key: str) -> bool:
        if session.get("role") == "admin":
            return True
        u = self._user(session["username"])
        if not u:
            return False
        sel = u.get("cameras", "all")
        return sel == "all" or key in (sel or [])

    # -- run -------------------------------------------------------------------------------
    async def run(self) -> None:
        self._stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if not IS_WINDOWS:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, self._stop.set)
                except (NotImplementedError, RuntimeError):
                    pass
        cert, key = self.tls_files()
        ctx = None
        if cert:
            try:
                ctx = make_server_ssl(cert, key)
                self.tls_cert = cert
            except (OSError, ValueError) as e:
                log.error("cannot load the TLS certificate %s: %s", cert, e)
        await self.web.start(self.cfg.get("bind", "0.0.0.0"), int(self.cfg.get("port", DEFAULT_PORT)), ctx,
                             also_plain=True)
        log.info("Eris CCTV server %s listening on port %s (%s)", __version__, self.cfg.get("port"),
                 "http + https/TLS on the same port" if ctx else "http only - camera connections are NOT encrypted")
        write_pid("server")
        try:
            await self._stop.wait()
        finally:
            for conn in list(self.agents.values()):
                conn.sock.abort()
            await self.web.stop()
            save_json(self.state_path, self.known)
            remove_pid("server")

    def tls_files(self) -> tuple:
        """(cert, key) for TLS; mode 'auto' creates a self-signed pair once and keeps it."""
        tls = self.cfg["tls"]
        if tls["mode"] == "off":
            return "", ""
        if tls["mode"] == "files":
            if certs.usable(tls["cert"], tls["key"]):
                return tls["cert"], tls["key"]
            log.error("TLS certificate %s / key %s cannot be loaded - using a self-signed certificate instead",
                      tls["cert"], tls["key"])
        cert, key = certs.default_paths()
        if not certs.usable(cert, key):
            try:
                certs.make_self_signed(self.cfg.get("public_address", ""), cert, key)
                log.info("created a self-signed TLS certificate: %s", cert)
            except Exception as e:  # noqa: BLE001
                log.error("cannot create a TLS certificate (%s) - camera connections stay unencrypted", e)
                return "", ""
        return cert, key

    # -- camera bookkeeping ----------------------------------------------------------------
    def camera_list(self, session: dict) -> list:
        out = []
        for agent_id, info in self.known.items():
            conn = self.agents.get(agent_id)
            for cam in info.get("cameras", []):
                key = "%s/%s" % (agent_id, cam["id"])
                if not self._allowed(session, key):
                    continue
                live = None
                if conn is not None and cam["id"] in conn.by_id:
                    live = conn.cams.get(conn.by_id[cam["id"]])
                item = {"key": key, "agent_id": agent_id, "agent_name": info.get("name", agent_id),
                        "id": cam["id"], "name": cam.get("name", cam["id"]),
                        "agent_online": conn is not None, "online": bool(live and live.get("online")),
                        "state": (live or {}).get("state", "offline"), "error": (live or {}).get("error", ""),
                        "audio": bool((live or cam).get("audio")), "width": (live or {}).get("width", 0),
                        "height": (live or {}).get("height", 0), "recording": bool((live or {}).get("recording")),
                        "last_seen": info.get("last_seen", 0), "viewers": len(self.viewers.get(key, ()))}
                out.append(item)
        out.sort(key=lambda c: (c["agent_name"].lower(), c["name"].lower()))
        return out

    def _meta(self, key: str) -> dict:
        agent_id, cam_id = key.split("/", 1)
        info = self.known.get(agent_id, {})
        conn = self.agents.get(agent_id)
        cam = None
        if conn and cam_id in conn.by_id:
            cam = conn.cams.get(conn.by_id[cam_id])
        name = (cam or {}).get("name") or next((c.get("name") for c in info.get("cameras", [])
                                                 if c["id"] == cam_id), cam_id)
        return {"type": "meta", "key": key, "name": name, "agent_name": info.get("name", agent_id),
                "online": bool(cam and cam.get("online")), "codec": (cam or {}).get("codec", ""),
                "audio": bool((cam or {}).get("audio")), "state": (cam or {}).get("state", "offline"),
                "error": (cam or {}).get("error", "") if conn else "Camera system is offline"}

    async def _want(self, key: str) -> None:
        """Make sure the agent streams this camera (someone is watching)."""
        timer = self._unsub.pop(key, None)
        if timer:
            timer.cancel()
        agent_id, cam_id = key.split("/", 1)
        conn = self.agents.get(agent_id)
        if not conn or cam_id not in conn.by_id:
            return
        slot = conn.by_id[cam_id]
        if slot in conn.subscribed:
            return
        conn.subscribed.add(slot)
        conn.inits.pop(slot, None)
        try:
            await conn.sock.send_json({"type": "subscribe", "slot": slot})
        except WebSocketClosed:
            pass

    def _release(self, key: str) -> None:
        if self.viewers.get(key):
            return

        async def later():
            await asyncio.sleep(GRACE_SECONDS)
            self._unsub.pop(key, None)
            if self.viewers.get(key):
                return
            agent_id, cam_id = key.split("/", 1)
            conn = self.agents.get(agent_id)
            if not conn or cam_id not in conn.by_id:
                return
            slot = conn.by_id[cam_id]
            conn.subscribed.discard(slot)
            conn.inits.pop(slot, None)
            try:
                await conn.sock.send_json({"type": "unsubscribe", "slot": slot})
            except WebSocketClosed:
                pass
        old = self._unsub.pop(key, None)
        if old:
            old.cancel()
        self._unsub[key] = asyncio.ensure_future(later())

    # -- agent connections -----------------------------------------------------------------
    def _fake_salt(self, username: str) -> str:
        return hmac.new(bytes.fromhex(self.cfg["fake_salt_key"]), username.encode(), hashlib.sha256).hexdigest()[:32]

    async def agent_session(self, req, sock) -> None:
        ip = self._client_ip(req)
        wait = self.agent_throttle.blocked_for(ip)
        if wait:
            await sock.send_json({"type": "error", "message": "Too many failed logins from your address; "
                                                               "try again in %d seconds" % wait})
            return
        try:
            hello = json.loads(await asyncio.wait_for(sock.recv(), 20))
        except (asyncio.TimeoutError, ValueError, TypeError):
            return
        if hello.get("type") != "hello":
            return
        username = str(hello.get("username", ""))[:64]
        agent_id = str(hello.get("agent_id", ""))
        cnonce = str(hello.get("cnonce", ""))[:64]
        if not re.match(r"^[A-Za-z0-9]{4,40}$", agent_id) or not cnonce:
            await sock.send_json({"type": "error", "message": "Bad hello"})
            return
        account = self._account(username)
        if account:
            salt, iters = account["secret"]["salt"], int(account["secret"]["iterations"])
        else:
            salt, iters = self._fake_salt(username), ITERATIONS
        nonce = secrets.token_hex(16)
        await sock.send_json({"type": "challenge", "salt": salt, "iterations": iters, "nonce": nonce,
                              "server_version": __version__})
        try:
            msg = json.loads(await asyncio.wait_for(sock.recv(), 60))
        except (asyncio.TimeoutError, ValueError, TypeError):
            return
        key = bytes.fromhex(account["secret"]["key"]) if account else b""
        if not account or msg.get("type") != "auth" or \
                not same(msg.get("proof", ""), proof(key, "agent", nonce, cnonce, agent_id)):
            self.agent_throttle.fail(ip)
            log.warning("camera login failed for %r from %s", username, ip)
            await asyncio.sleep(1)
            await sock.send_json({"type": "error", "message": "Wrong username or password"})
            return
        self.agent_throttle.success(ip)
        await sock.send_json({"type": "welcome", "server_name": self.cfg.get("name", ""),
                              "proof": proof(key, "server", nonce, cnonce, agent_id),
                              "server_version": __version__, "tls": bool(self.tls_cert)})
        if hello.get("probe"):
            return  # connection test from a camera panel / setup wizard
        name = valid_name(hello.get("agent_name") or agent_id)
        conn = AgentConn(sock, agent_id, name, username, ip, str(hello.get("version", ""))[:20], req.secure)
        old = self.agents.get(agent_id)
        if old is not None:
            old.sock.abort()
        self.agents[agent_id] = conn
        info = self.known.setdefault(agent_id, {"cameras": []})
        info.update({"name": name, "ip": ip, "account": username, "last_seen": int(time.time()),
                     "version": conn.version})
        self._schedule_state_save()
        log.info("camera system %s (%s) connected from %s as %s, %s", name, agent_id, ip, username,
                 "TLS" if req.secure else "unencrypted")
        watchdog = asyncio.ensure_future(self._agent_watchdog(conn))
        try:
            while True:
                msg = await sock.recv()
                if isinstance(msg, bytes):
                    self._agent_media(conn, msg)
                else:
                    try:
                        data = json.loads(msg)
                    except ValueError:
                        continue
                    if data.get("type") == "cameras":
                        await self._agent_cameras(conn, data)
        except WebSocketClosed:
            pass
        finally:
            watchdog.cancel()
            if self.agents.get(agent_id) is conn:
                del self.agents[agent_id]
                info["last_seen"] = int(time.time())
                self._schedule_state_save()
                for cam_id in conn.by_id:
                    key = "%s/%s" % (agent_id, cam_id)
                    for v in self.viewers.get(key, ()):
                        v.send_meta(self._meta(key))
            log.info("camera system %s (%s) disconnected", name, agent_id)

    async def _agent_watchdog(self, conn: AgentConn) -> None:
        while True:
            await asyncio.sleep(25)
            if time.monotonic() - conn.sock.last_recv > 90:
                log.info("camera system %s timed out", conn.name)
                conn.sock.abort()
                return
            try:
                await conn.sock.ping()
            except WebSocketClosed:
                return

    async def _agent_cameras(self, conn: AgentConn, data: dict) -> None:
        cams = {}
        by_id = {}
        for c in data.get("cameras", [])[:64]:
            try:
                slot = int(c["slot"])
                cid = str(c["id"])[:40]
            except (KeyError, ValueError, TypeError):
                continue
            c["name"] = valid_name(c.get("name") or cid)
            cams[slot] = c
            by_id[cid] = slot
        if data.get("agent_name"):
            conn.name = valid_name(data["agent_name"]) or conn.name
        previous = conn.by_id
        conn.cams, conn.by_id = cams, by_id
        for slot in list(conn.subscribed):
            if slot not in cams:
                conn.subscribed.discard(slot)
                conn.inits.pop(slot, None)
        info = self.known.setdefault(conn.agent_id, {})
        info["name"] = conn.name
        info["last_seen"] = int(time.time())
        info["cameras"] = [{"id": c["id"], "name": c["name"], "audio": bool(c.get("audio"))}
                           for c in sorted(cams.values(), key=lambda x: x["slot"])]
        self._schedule_state_save()
        for cid in set(previous) | set(by_id):
            key = "%s/%s" % (conn.agent_id, cid)
            viewers = self.viewers.get(key)
            if not viewers:
                continue
            meta = self._meta(key)
            for v in viewers:
                if not meta["online"] or not v.primed:
                    v.send_meta(meta)
            if cid in by_id:
                await self._want(key)

    def _agent_media(self, conn: AgentConn, msg: bytes) -> None:
        if len(msg) < 3:
            return
        kind, slot = struct.unpack_from(">BH", msg, 0)
        payload = msg[3:]
        conn.bytes_in += len(msg)
        cam = conn.cams.get(slot)
        if cam is None:
            return
        key = "%s/%s" % (conn.agent_id, cam["id"])
        viewers = self.viewers.get(key, ())
        if kind == 1:
            conn.inits[slot] = payload
            conn.keys[slot] = KeyframeDetector(payload)
            if viewers:
                meta = self._meta(key)
                for v in viewers:
                    v.on_init(meta, payload)
        elif kind == 2:
            det = conn.keys.get(slot)
            is_key = det.is_key(payload) if det else True
            for v in viewers:
                v.on_frag(payload, is_key)

    @staticmethod
    def _client_ip(req) -> str:
        return req.remote

    # -- browser viewers -------------------------------------------------------------------
    async def view_session(self, req, sock) -> None:
        if not req.session:
            await sock.send_json({"type": "error", "message": "Login required"})
            return
        key = "%s/%s" % (req.params["agent"], req.params["cam"])
        if not self._allowed(req.session, key):
            await sock.send_json({"type": "error", "message": "Not allowed"})
            return
        viewer = Viewer()
        self.viewers.setdefault(key, set()).add(viewer)
        viewer.send_meta(self._meta(key))
        agent_id, cam_id = key.split("/", 1)
        conn = self.agents.get(agent_id)
        if conn and cam_id in conn.by_id:
            slot = conn.by_id[cam_id]
            if slot in conn.subscribed and slot in conn.inits:
                viewer.on_init(self._meta(key), conn.inits[slot])
        await self._want(key)

        async def drain_input():
            try:
                while True:
                    await sock.recv()
            except WebSocketClosed:
                return
        reader = asyncio.ensure_future(drain_input())
        try:
            while not reader.done():
                getter = asyncio.ensure_future(viewer.q.get())
                done, _ = await asyncio.wait({getter, reader}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    getter.cancel()
                    await sock.ping()
                    continue
                if getter not in done:
                    getter.cancel()
                    break
                kind, data = getter.result()
                if kind == 0:
                    await sock.send_text(data)
                else:
                    await sock.send_bytes(bytes([kind]) + data)
        finally:
            reader.cancel()
            group = self.viewers.get(key)
            if group is not None:
                group.discard(viewer)
                if not group:
                    self.viewers.pop(key, None)
            self._release(key)

    # -- routes ----------------------------------------------------------------------------
    def connection_info(self) -> dict:
        return {"address": self.cfg.get("public_address", ""), "port": self.cfg.get("port"),
                "tls": bool(self.tls_cert), "tls_mode": self.cfg["tls"]["mode"],
                "fingerprint": certs.fingerprint_file(self.tls_cert) if self.tls_cert else "",
                "accounts": [a["username"] for a in self.cfg["camera_accounts"]]}

    def _routes(self) -> None:
        r = self.web.route

        @r("GET", "/api/session")
        def session(req):
            s = req.session
            return {"logged_in": bool(s), "username": s["username"] if s else "",
                    "role": s["role"] if s else "", "app": "server",
                    "name": self.cfg.get("name", "") if s else "", "version": __version__}

        @r("GET", "/api/cameras")
        def cameras(req):
            s = need_login(req)
            return {"cameras": self.camera_list(s), "server_name": self.cfg.get("name", "")}

        @r("GET", "/api/agents")
        def agents(req):
            need_admin(req)
            out = []
            for agent_id, info in self.known.items():
                conn = self.agents.get(agent_id)
                out.append({"id": agent_id, "name": info.get("name", agent_id), "online": conn is not None,
                            "ip": conn.ip if conn else info.get("ip", ""), "account": info.get("account", ""),
                            "version": info.get("version", ""), "last_seen": info.get("last_seen", 0),
                            "connected_at": int(conn.connected_at) if conn else 0,
                            "bytes_in": conn.bytes_in if conn else 0,
                            "rate_in": conn.rate_in() if conn else 0,
                            "secure": conn.secure if conn else False,
                            "cameras": len(info.get("cameras", [])),
                            "streaming": len(conn.subscribed) if conn else 0})
            out.sort(key=lambda a: (not a["online"], a["name"].lower()))
            return {"agents": out}

        @r("DELETE", "/api/agents/{aid}")
        def forget_agent(req):
            need_admin(req)
            aid = req.params["aid"]
            conn = self.agents.get(aid)
            if conn:
                raise HttpError(409, "This camera system is connected. Remove its camera account or "
                                     "remove this server on the camera system first.")
            self.known.pop(aid, None)
            save_json(self.state_path, self.known)
            return {"ok": True}

        # camera accounts (what webcam systems log in with)
        @r("GET", "/api/accounts")
        def accounts(req):
            need_admin(req)
            items = []
            for a in self.cfg["camera_accounts"]:
                online = [c.name for c in self.agents.values() if c.account == a["username"]]
                items.append({"username": a["username"], "note": a.get("note", ""), "created": a.get("created", 0),
                              "connected": online})
            return {"accounts": items, "connection": self.connection_info()}

        @r("POST", "/api/accounts")
        async def add_account(req):
            need_admin(req)
            d = req.json()
            name = valid_user(d.get("username"))
            if self._account(name):
                raise HttpError(409, "That camera account already exists")
            pw = check_password(d.get("password"))
            loop = asyncio.get_running_loop()
            secret = await loop.run_in_executor(None, make_secret, pw)
            self.cfg["camera_accounts"].append({"username": name, "secret": secret, "created": int(time.time()),
                                                "note": valid_name(d.get("note", ""), 80)})
            self.save()
            return {"ok": True}

        @r("PUT", "/api/accounts/{name}")
        async def edit_account(req):
            need_admin(req)
            a = self._account(req.params["name"])
            if not a:
                raise HttpError(404, "Camera account not found")
            d = req.json()
            if d.get("password"):
                loop = asyncio.get_running_loop()
                a["secret"] = await loop.run_in_executor(None, make_secret, check_password(d["password"]))
                for c in list(self.agents.values()):
                    if c.account == a["username"]:
                        c.sock.abort()
            if "note" in d:
                a["note"] = valid_name(d["note"], 80)
            self.save()
            return {"ok": True}

        @r("DELETE", "/api/accounts/{name}")
        def del_account(req):
            need_admin(req)
            name = req.params["name"]
            if not self._account(name):
                raise HttpError(404, "Camera account not found")
            self.cfg["camera_accounts"] = [a for a in self.cfg["camera_accounts"] if a["username"] != name]
            self.save()
            for c in list(self.agents.values()):
                if c.account == name:
                    c.sock.abort()
            return {"ok": True}

        # panel users
        @r("GET", "/api/users")
        def users(req):
            need_admin(req)
            return {"users": [{"username": u["username"], "role": u.get("role", "viewer"),
                               "cameras": u.get("cameras", "all")} for u in self.cfg["users"]],
                    "cameras": self.camera_list({"role": "admin", "username": ""})}

        @r("POST", "/api/users")
        async def add_user(req):
            need_admin(req)
            d = req.json()
            name = valid_user(d.get("username"))
            if self._user(name):
                raise HttpError(409, "That user already exists")
            pw = check_password(d.get("password"))
            loop = asyncio.get_running_loop()
            secret = await loop.run_in_executor(None, make_secret, pw)
            role = "admin" if d.get("role") == "admin" else "viewer"
            cams = "all" if d.get("cameras", "all") == "all" else [str(x) for x in d.get("cameras") or []]
            self.cfg["users"].append({"username": name, "secret": secret, "role": role, "cameras": cams})
            self.save()
            return {"ok": True}

        @r("PUT", "/api/users/{name}")
        async def edit_user(req):
            s = need_admin(req)
            u = self._user(req.params["name"])
            if not u:
                raise HttpError(404, "User not found")
            d = req.json()
            if d.get("password"):
                loop = asyncio.get_running_loop()
                u["secret"] = await loop.run_in_executor(None, make_secret, check_password(d["password"]))
                self.auth.sessions.drop_user(u["username"], keep=req.session_token)
            if d.get("role") in ("admin", "viewer"):
                if d["role"] == "viewer" and u.get("role") == "admin" and self._admins() <= 1:
                    raise HttpError(400, "Keep at least one administrator")
                if u["username"] == s["username"] and d["role"] != "admin":
                    raise HttpError(400, "You cannot remove your own administrator role")
                u["role"] = d["role"]
            if "cameras" in d:
                u["cameras"] = "all" if d["cameras"] == "all" else [str(x) for x in d["cameras"] or []]
            self.save()
            return {"ok": True}

        @r("DELETE", "/api/users/{name}")
        def del_user(req):
            s = need_admin(req)
            u = self._user(req.params["name"])
            if not u:
                raise HttpError(404, "User not found")
            if u["username"] == s["username"]:
                raise HttpError(400, "You cannot delete yourself")
            if u.get("role") == "admin" and self._admins() <= 1:
                raise HttpError(400, "Keep at least one administrator")
            self.cfg["users"] = [x for x in self.cfg["users"] if x["username"] != u["username"]]
            self.save()
            self.auth.sessions.drop_user(u["username"])
            return {"ok": True}

        @r("POST", "/api/password")
        async def password(req):
            s = need_login(req)
            d = req.json()
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, self._verify, s["username"], str(d.get("current", "")))
            if not ok:
                raise HttpError(400, "Current password is wrong")
            u = self._user(s["username"])
            u["secret"] = await loop.run_in_executor(None, make_secret, check_password(d.get("password")))
            self.save()
            self.auth.sessions.drop_user(s["username"], keep=req.session_token)
            return {"ok": True}

        @r("GET", "/api/settings")
        def settings(req):
            need_admin(req)
            info = self.connection_info()
            info.update({"name": self.cfg.get("name", ""), "bind": self.cfg.get("bind"),
                         "server_id": self.cfg.get("server_id"), "version": __version__,
                         "uptime": int(time.time() - self.started), "tls_cert": self.tls_cert})
            return info

        @r("PUT", "/api/settings")
        def set_settings(req):
            need_admin(req)
            d = req.json()
            if "name" in d:
                self.cfg["name"] = valid_name(d["name"]) or self.cfg["name"]
            if "public_address" in d:
                self.cfg["public_address"] = valid_name(d["public_address"], 255)
            self.save()
            return {"ok": True}

        @r("GET", "/api/logs")
        def logs(req):
            need_admin(req)
            return {"lines": log_tail("server", 300)}

        self.web.websocket("/agent/ws")(self.agent_session)
        self.web.websocket("/api/view/{agent}/{cam}")(self.view_session)

    def _admins(self) -> int:
        return sum(1 for u in self.cfg["users"] if u.get("role") == "admin")


def run_server() -> None:
    server = Server()
    if not server.cfg["users"]:
        log.error("No panel user yet. Run 'eris-cctv' and set this system up as a server.")
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
