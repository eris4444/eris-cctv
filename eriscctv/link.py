"""Agent side of the agent -> server connection (outbound, so cameras need no public IP)."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import secrets
import socket
import ssl
import struct
import time

from . import __version__
from .auth import derive, proof, same
from .ws import HandshakeError, WebSocketClosed, connect

log = logging.getLogger("link")

KIND_INIT, KIND_FRAG = 1, 2
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_LAG = 3.0            # seconds a video fragment may wait to be sent before it is skipped
AGENT_PATH = "/agent/ws"
TLS_MODES = ("auto", "on", "off")
AUTO_TLS_TIMEOUT = 10    # how long "auto" waits for a TLS handshake before trying plain
PLAIN_RETRY_TLS = 600    # after a server turned out to have no TLS, try TLS again this much later
SEND_BUFFER = 128 * 1024  # fixed socket send buffer: OS auto-tuning could hold many seconds of video


class AuthError(Exception):
    pass


class Reconnect(Exception):
    """End the session and connect again right away (e.g. to switch to TLS)."""


def cert_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def client_ssl(verify: str):
    if verify == "ca":
        # ERIS_CCTV_CA_FILE: trust this CA bundle instead of the system's (tests, private CAs)
        return ssl.create_default_context(cafile=os.environ.get("ERIS_CCTV_CA_FILE") or None)
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


def tls_mode(cfg: dict) -> str:
    mode = cfg.get("tls_mode")
    if mode in TLS_MODES:
        return mode
    return "on" if cfg.get("tls") is True else "auto"


async def _open_tls(cfg: dict, timeout: float):
    verify = cfg.get("tls_verify") or "pin"
    ws = await connect(cfg["address"], int(cfg["port"]), AGENT_PATH, ssl_ctx=client_ssl(verify), timeout=timeout)
    sslobj = ws.writer.get_extra_info("ssl_object")
    der = sslobj.getpeercert(binary_form=True) if sslobj else b""
    fp = cert_fingerprint(der or b"")
    if verify == "pin" and cfg.get("tls_pin") and not same(fp, cfg["tls_pin"]):
        ws.abort()
        # A CDN (ArvanCloud, Cloudflare) or Let's Encrypt renews its certificate every few months. A new
        # certificate that a public CA vouches for, for this very name, is followed; anything else is refused.
        try:
            ws = await connect(cfg["address"], int(cfg["port"]), AGENT_PATH, ssl_ctx=client_ssl("ca"),
                               timeout=timeout)
        except ssl.SSLCertVerificationError:
            raise AuthError("The server's TLS certificate changed (expected %s..., got %s...). If you reinstalled "
                            "the server, open this server in the camera panel and choose \"Forget pinned "
                            "certificate\"." % (cfg["tls_pin"][:16], fp[:16]))
        der = ws.writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        fp = cert_fingerprint(der or b"")
        log.info("server %s:%s renewed its publicly trusted certificate - pin updated to %s...",
                 cfg["address"], cfg["port"], fp[:16])
        cfg["tls_pin"] = fp
    return ws, fp


async def open_ws(cfg: dict, plain_first: bool = False):
    """Connect to a server. Returns (ws, certificate fingerprint, encrypted?).

    tls_mode "auto" uses TLS whenever the server offers it and falls back to plain only for servers
    that never had TLS (no pinned certificate yet). Many networks throttle unencrypted uploads, so
    the camera stream should travel inside TLS.
    """
    mode = tls_mode(cfg)
    if mode == "on" or (mode == "auto" and (cfg.get("tls_pin") or not plain_first)):
        try:
            ws, fp = await _open_tls(cfg, 15 if mode == "on" or cfg.get("tls_pin") else AUTO_TLS_TIMEOUT)
            _limit_send_buffer(ws)
            return ws, fp, True
        except ssl.SSLCertVerificationError:
            raise
        except (ssl.SSLError, asyncio.TimeoutError, ConnectionError, HandshakeError, OSError) as e:
            if mode == "on" or cfg.get("tls_pin"):
                raise
            log.info("server %s:%s: no TLS (%s) - trying an unencrypted connection", cfg["address"], cfg["port"],
                     describe_error(e))
    ws = await connect(cfg["address"], int(cfg["port"]), AGENT_PATH)
    _limit_send_buffer(ws)
    return ws, "", False


def _limit_send_buffer(ws) -> None:
    """Keep buffers below the app small so a slow uplink makes the sender wait (and skip stale
    fragments) instead of hiding seconds of video in the OS / asyncio TLS buffers (512 KiB default)."""
    sock = ws.writer.get_extra_info("socket")
    try:
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)
        ws.writer.transport.set_write_buffer_limits(high=64 * 1024)
    except (OSError, AttributeError, NotImplementedError):
        pass


async def test_server(cfg: dict, agent_id: str, agent_name: str) -> dict:
    """One-off connection test used by the setup wizard and the panel."""
    ws = None
    try:
        ws, fp, secure = await open_ws(cfg)
        welcome = await handshake(ws, cfg, agent_id, agent_name, {}, probe=True)
        return {"ok": True, "server_name": welcome.get("server_name", ""), "fingerprint": fp, "secure": secure,
                "server_version": welcome.get("server_version", ""), "server_tls": bool(welcome.get("tls"))}
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
        self.secure = False
        self.server_tls = False
        self.subscribed = set()
        self.primed = set()
        self.ws = None
        self._task = None
        self._queue = collections.deque()  # see _drop_gop
        self._queued = 0
        self._gap_tail = set()  # camera slots whose queued stream ends in a gap (wait for a keyframe)
        self._wake = asyncio.Event()
        self._key_cache: dict = {}
        self._cams_dirty = asyncio.Event()
        self._plain_until = 0.0
        self._rate = (time.monotonic(), 0, 0)

    def rate_out(self) -> int:
        t0, b0, rate = self._rate
        now = time.monotonic()
        if now - t0 >= 1:
            rate = int((self.bytes_sent - b0) / (now - t0))
            self._rate = (now, self.bytes_sent, rate)
        return rate

    def public(self) -> dict:
        c = self.cfg
        connected = self.state == "connected"
        return {"id": self.id, "address": c["address"], "port": c["port"], "username": c["username"],
                "tls_mode": tls_mode(c), "tls_verify": c.get("tls_verify", "pin"),
                "tls_pin": c.get("tls_pin", ""), "enabled": bool(c.get("enabled", True)),
                "cameras": c.get("cameras", "all"), "state": self.state, "error": self.error,
                "server_name": self.server_name, "bytes_sent": self.bytes_sent, "dropped": self.dropped,
                "rate": self.rate_out() if connected else 0, "secure": connected and self.secure,
                "server_tls": self.server_tls,
                "connected_since": int(self.connected_since) if connected else 0,
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
            self.connected_since = 0.0
            delay = None
            try:
                await self._session()
                backoff = 3
            except Reconnect as e:
                self.state, self.error = "connecting", ""
                log.info("server %s:%s: %s", self.cfg["address"], self.cfg["port"], e)
                delay = 1
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
            if delay is None:
                if self.connected_since and backoff < 60:
                    backoff = 2  # the link was up: come back quickly, viewers are probably waiting
                delay = backoff
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(delay)

    def _detach(self) -> None:
        for cam in self.agent.cameras.values():
            cam.remove(self)
        self.subscribed.clear()
        self.primed.clear()
        self._queue.clear()
        self._queued = 0
        self._gap_tail.clear()

    async def _session(self) -> None:
        plain_first = time.monotonic() < self._plain_until
        pin_before = self.cfg.get("tls_pin", "")
        ws, fp, secure = await open_ws(self.cfg, plain_first=plain_first)
        self.ws, self.secure = ws, secure
        if not secure and tls_mode(self.cfg) == "auto":
            self._plain_until = time.monotonic() + PLAIN_RETRY_TLS
        welcome = await handshake(ws, self.cfg, self.agent.agent_id, self.agent.name, self._key_cache)
        if secure and self.cfg.get("tls_verify", "pin") == "pin":
            if not self.cfg.get("tls_pin"):
                self.cfg["tls_pin"] = fp  # trust on first use, only after the server proved the password
            if self.cfg["tls_pin"] != pin_before:
                self.agent.save()
        self.server_tls = bool(welcome.get("tls")) or secure
        if self.server_tls and not secure and tls_mode(self.cfg) == "auto":
            self._plain_until = 0.0
            if plain_first:
                # TLS was skipped because this server had none before - it was updated: switch now
                raise Reconnect("the server offers TLS now - reconnecting encrypted")
        self.server_name = welcome.get("server_name", "")
        self.state, self.error = "connected", ""
        self.connected_since = time.time()
        log.info("connected to server %s:%s (%s), %s", self.cfg["address"], self.cfg["port"], self.server_name,
                 "TLS" if secure else "NOT encrypted")
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
                self._gap_tail.add(cam.slot)  # start the server's copy at the next keyframe
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
            self._gap_tail.add(cam.slot)
            self.primed.add(cam.id)

    def on_frag(self, cam, data: bytes, key: bool = True) -> None:
        if cam.id in self.primed:
            self._enqueue(KIND_FRAG, cam.slot, data, key=key)

    # Queue items: (queued at, kind, slot, starts with a keyframe, framed bytes). Fragments are only
    # dropped together with the rest of their group of pictures: a decoder that misses a fragment shows
    # garbage until the next keyframe, so the stream resumes exactly there.
    def _drop_gop(self, items: list, i: int) -> None:
        slot = items[i][2]
        for j in range(i, len(items)):
            it = items[j]
            if it is None or it[2] != slot:
                continue
            if j > i and (it[1] == KIND_INIT or it[3]):
                return  # the camera's stream continues cleanly from here
            if it[1] == KIND_FRAG:
                self.dropped += 1
                self._queued -= len(it[4]) - 3
                items[j] = None
        self._gap_tail.add(slot)  # nothing clean queued yet: drop new fragments until a keyframe

    def _prune(self) -> None:
        """Forget fragments that waited longer than MAX_LAG while the sender was stuck on a slow link."""
        now = time.monotonic()
        if not self._queue or now - self._queue[0][0] <= MAX_LAG:
            return
        items = list(self._queue)
        for i, it in enumerate(items):
            if it is not None and it[1] == KIND_FRAG and now - it[0] > MAX_LAG:
                self._drop_gop(items, i)
        self._queue = collections.deque(it for it in items if it is not None)

    def _enqueue(self, kind: int, slot: int, data: bytes, force: bool = False, key: bool = True) -> None:
        self._prune()
        if kind == KIND_INIT:
            self._gap_tail.discard(slot)
        elif slot in self._gap_tail:
            if not key:
                self.dropped += 1
                return
            self._gap_tail.discard(slot)
        if not force and self._queued + len(data) > MAX_QUEUE_BYTES:
            self.dropped += 1
            self._gap_tail.add(slot)
            return
        self._queue.append((time.monotonic(), kind, slot, key, struct.pack(">BH", kind, slot) + data))
        self._queued += len(data)
        self._wake.set()

    async def _sender(self) -> None:
        """Sends queued media. On a link slower than the camera, fragments that waited longer than
        MAX_LAG are skipped up to the next keyframe, so viewers stay live instead of falling minutes
        behind."""
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._queue and self.ws:
                    self._prune()
                    if not self._queue:
                        break
                    item = self._queue.popleft()[4]
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
