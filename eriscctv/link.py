"""Agent side of the agent -> server connection (outbound, so cameras need no public IP)."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import secrets
import ssl
import struct
import time

from . import __version__
from .auth import derive, proof, same
from .ws import HandshakeError, WebSocketClosed, connect

log = logging.getLogger("link")

KIND_INIT, KIND_FRAG = 1, 2
MAX_QUEUE_BYTES = 8 * 1024 * 1024
AGENT_PATH = "/agent/ws"


class AuthError(Exception):
    pass


def cert_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def client_ssl(verify: str):
    if verify == "ca":
        return ssl.create_default_context()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def handshake(ws, cfg: dict, agent_id: str, agent_name: str, key_cache: dict, probe: bool = False) -> dict:
    """Challenge/response login. The password itself never crosses the network.

    probe=True only checks the login; the server does not register (or replace) the agent.
    """
    cnonce = secrets.token_hex(16)
    await ws.send_json({"type": "hello", "username": cfg["username"], "agent_id": agent_id,
                        "agent_name": agent_name, "version": __version__, "cnonce": cnonce, "probe": probe})
    msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
    if msg.get("type") == "error":
        raise AuthError(msg.get("message") or "refused by server")
    if msg.get("type") != "challenge":
        raise AuthError("unexpected answer from server")
    salt, iters, nonce = msg["salt"], int(msg["iterations"]), msg["nonce"]
    ck = (cfg["password"], salt, iters)
    key = key_cache.get(ck)
    if key is None:
        loop = asyncio.get_running_loop()
        key = await loop.run_in_executor(None, derive, cfg["password"], bytes.fromhex(salt), iters)
        key_cache.clear()
        key_cache[ck] = key
    await ws.send_json({"type": "auth", "proof": proof(key, "agent", nonce, cnonce, agent_id)})
    msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
    if msg.get("type") == "error":
        raise AuthError(msg.get("message") or "Wrong username or password")
    if msg.get("type") != "welcome":
        raise AuthError("unexpected answer from server")
    if not same(msg.get("proof", ""), proof(key, "server", nonce, cnonce, agent_id)):
        raise AuthError("server could not prove it knows the password (wrong server?)")
    return msg


async def open_ws(cfg: dict):
    tls = bool(cfg.get("tls"))
    verify = cfg.get("tls_verify") or "pin"
    ctx = client_ssl(verify) if tls else None
    ws = await connect(cfg["address"], int(cfg["port"]), AGENT_PATH, ssl_ctx=ctx)
    fp = ""
    if tls:
        sslobj = ws.writer.get_extra_info("ssl_object")
        der = sslobj.getpeercert(binary_form=True) if sslobj else b""
        fp = cert_fingerprint(der or b"")
        if verify == "pin" and cfg.get("tls_pin") and not same(fp, cfg["tls_pin"]):
            ws.abort()
            raise AuthError("TLS certificate changed! Expected %s..., got %s..." % (cfg["tls_pin"][:16], fp[:16]))
    return ws, fp


async def test_server(cfg: dict, agent_id: str, agent_name: str) -> dict:
    """One-off connection test used by the setup wizard and the panel."""
    ws = None
    try:
        ws, fp = await open_ws(cfg)
        welcome = await handshake(ws, cfg, agent_id, agent_name, {}, probe=True)
        return {"ok": True, "server_name": welcome.get("server_name", ""), "fingerprint": fp}
    except AuthError as e:
        return {"ok": False, "error": str(e)}
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "error": "TLS certificate not trusted: %s" % e.verify_message, "tls_untrusted": True}
    except (OSError, asyncio.TimeoutError, HandshakeError, WebSocketClosed, ValueError, KeyError) as e:
        return {"ok": False, "error": describe_error(e)}
    finally:
        if ws:
            await ws.close()


def describe_error(e: Exception) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "Connection timed out (check address, port and firewall)"
    if isinstance(e, ConnectionRefusedError):
        return "Connection refused (server not running or wrong port)"
    if isinstance(e, ssl.SSLError):
        return "TLS error: %s (is TLS enabled on the server?)" % (e.reason or e)
    if isinstance(e, HandshakeError):
        return "Not an Eris CCTV server: %s" % e
    text = str(e) or e.__class__.__name__
    if "getaddrinfo" in text or "Name or service not known" in text or "nodename" in text:
        return "Cannot resolve address (check the IP / domain)"
    return text


class ServerLink:
    """Keeps one connection to one server alive and streams the cameras it asks for."""

    def __init__(self, agent, cfg: dict):
        self.agent = agent
        self.cfg = cfg
        self.id = cfg["id"]
        self.state = "stopped"
        self.error = ""
        self.server_name = ""
        self.connected_since = 0.0
        self.last_attempt = 0.0
        self.bytes_sent = 0
        self.dropped = 0
        self.subscribed = set()
        self.primed = set()
        self.ws = None
        self._task = None
        self._queue = collections.deque()
        self._queued = 0
        self._wake = asyncio.Event()
        self._key_cache: dict = {}
        self._cams_dirty = asyncio.Event()

    def public(self) -> dict:
        c = self.cfg
        return {"id": self.id, "address": c["address"], "port": c["port"], "username": c["username"],
                "tls": bool(c.get("tls")), "tls_verify": c.get("tls_verify", "pin"),
                "tls_pin": c.get("tls_pin", ""), "enabled": bool(c.get("enabled", True)),
                "cameras": c.get("cameras", "all"), "state": self.state, "error": self.error,
                "server_name": self.server_name, "bytes_sent": self.bytes_sent, "dropped": self.dropped,
                "connected_since": int(self.connected_since) if self.state == "connected" else 0,
                "watching": sorted(self.subscribed)}

    def shares(self, cam_id: str) -> bool:
        sel = self.cfg.get("cameras", "all")
        return sel == "all" or cam_id in (sel or [])

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if self.ws:
            self.ws.abort()
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._detach()
        self.state = "stopped"

    def cameras_changed(self) -> None:
        self._cams_dirty.set()

    async def _loop(self) -> None:
        backoff = 3
        while True:
            if not self.cfg.get("enabled", True):
                self.state, self.error = "disabled", ""
                return
            self.state = "connecting"
            self.last_attempt = time.time()
            try:
                await self._session()
                backoff = 3
            except AuthError as e:
                self.state, self.error = "error", str(e)
                log.warning("server %s:%s: %s", self.cfg["address"], self.cfg["port"], e)
                backoff = 60
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any network failure means reconnect
                self.state, self.error = "error", describe_error(e)
                log.info("server %s:%s: %s", self.cfg["address"], self.cfg["port"], self.error)
            finally:
                self._detach()
                if self.ws:
                    self.ws.abort()
                    self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _detach(self) -> None:
        for cam in self.agent.cameras.values():
            cam.remove(self)
        self.subscribed.clear()
        self.primed.clear()
        self._queue.clear()
        self._queued = 0

    async def _session(self) -> None:
        ws, fp = await open_ws(self.cfg)
        self.ws = ws
        if self.cfg.get("tls") and self.cfg.get("tls_verify", "pin") == "pin" and not self.cfg.get("tls_pin"):
            self.cfg["tls_pin"] = fp
            self.agent.save()
        welcome = await handshake(ws, self.cfg, self.agent.agent_id, self.agent.name, self._key_cache)
        self.server_name = welcome.get("server_name", "")
        self.state, self.error = "connected", ""
        self.connected_since = time.time()
        log.info("connected to server %s:%s (%s)", self.cfg["address"], self.cfg["port"], self.server_name)
        for cam in self.agent.cameras.values():
            cam.add(self)
        await self._send_cameras()
        tasks = [asyncio.ensure_future(self._sender()), asyncio.ensure_future(self._pinger()),
                 asyncio.ensure_future(self._cam_updates())]
        try:
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                self._handle(data)
        except WebSocketClosed as e:
            raise ConnectionError("connection closed: %s" % e)
        finally:
            for t in tasks:
                t.cancel()
            self.state = "error" if self.state == "connected" else self.state

    def _handle(self, data: dict) -> None:
        t = data.get("type")
        cam = self._cam_by_slot(data.get("slot"))
        if t == "subscribe" and cam and self.shares(cam.id):
            self.subscribed.add(cam.id)
            self.primed.discard(cam.id)
            if cam.init:
                self._enqueue(KIND_INIT, cam.slot, cam.init, force=True)
                self.primed.add(cam.id)
        elif t == "unsubscribe" and cam:
            self.subscribed.discard(cam.id)
            self.primed.discard(cam.id)

    def _cam_by_slot(self, slot):
        for cam in self.agent.cameras.values():
            if cam.slot == slot:
                return cam
        return None

    # -- camera subscriber interface ---------------------------------------------------------
    def on_init(self, cam, data: bytes) -> None:
        if cam.id in self.subscribed:
            self._enqueue(KIND_INIT, cam.slot, data, force=True)
            self.primed.add(cam.id)

    def on_frag(self, cam, data: bytes) -> None:
        if cam.id in self.primed:
            self._enqueue(KIND_FRAG, cam.slot, data)

    def _enqueue(self, kind: int, slot: int, data: bytes, force: bool = False) -> None:
        if not force and self._queued + len(data) > MAX_QUEUE_BYTES:
            self.dropped += 1
            return
        self._queue.append(struct.pack(">BH", kind, slot) + data)
        self._queued += len(data)
        self._wake.set()

    async def _sender(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._queue and self.ws:
                    item = self._queue.popleft()
                    self._queued -= len(item) - 3
                    await self.ws.send_bytes(item)
                    self.bytes_sent += len(item)
        except WebSocketClosed:
            if self.ws:
                self.ws.abort()

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(20)
            if self.ws is None:
                return
            if time.monotonic() - self.ws.last_recv > 75:
                log.info("server %s:%s stopped answering", self.cfg["address"], self.cfg["port"])
                self.ws.abort()
                return
            try:
                await self.ws.ping()
            except WebSocketClosed:
                return

    async def _cam_updates(self) -> None:
        try:
            while True:
                await self._cams_dirty.wait()
                await asyncio.sleep(0.5)
                self._cams_dirty.clear()
                await self._send_cameras()
        except WebSocketClosed:
            if self.ws:
                self.ws.abort()

    async def _send_cameras(self) -> None:
        cams = []
        for cam in self.agent.cameras.values():
            if not self.shares(cam.id):
                continue
            p = cam.public()
            cams.append({k: p[k] for k in ("id", "slot", "name", "online", "state", "error", "codec", "audio",
                                            "width", "height", "fps", "enabled")})
            cams[-1]["recording"] = cam.id in self.agent.storage.recorders
        if self.ws:
            await self.ws.send_json({"type": "cameras", "cameras": cams, "agent_name": self.agent.name})
