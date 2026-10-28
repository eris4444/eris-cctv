"""Camera -> server link through a network that throttles unencrypted uploads (as measured on real
Iranian home lines in 2026: plain uploads to foreign hosts ~5 KB/s and reset after ~20 s, TLS untouched).

Phases:
  A  encryption off  -> the video barely moves and the link gets reset (the old "Starting video..." bug)
  B  automatic       -> TLS, the video flows normally through the same throttling network
  C  automatic, server without TLS (like 1.0) -> falls back to an unencrypted link and still works
  D  TLS on a link slower than the camera -> old fragments are skipped, the queue stays short

Run:  python tests/throttle_test.py      (needs ffmpeg with libx264)
"""
import asyncio
import http.client
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="eriscctv-throttle-")
os.environ["ERIS_CCTV_HOME"] = HOME

from eriscctv import agent as agent_mod  # noqa: E402
from eriscctv import server as server_mod  # noqa: E402
from eriscctv.auth import make_secret  # noqa: E402
from eriscctv.util import config_path, find_ffmpeg, save_json  # noqa: E402
from eriscctv.ws import connect  # noqa: E402

SERVER_PORT = 18950
OLD_SERVER_PORT = 18951
PROXY_PORT = 18952
AGENT_PORT = 18195


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


class ThrottlingNetwork:
    """TCP proxy: TLS passes (optionally rate-limited); plain uploads are slowed and reset."""

    def __init__(self, target_port, plain_rate=5000, plain_reset_after=20, tls_rate=0):
        self.target_port = target_port
        self.plain_rate = plain_rate
        self.plain_reset_after = plain_reset_after
        self.tls_rate = tls_rate
        self.resets = 0
        self.sock = None
        self.task = None
        self.conns = set()

    async def start(self, port):
        # Raw socket reads in small pieces with a small receive buffer: like on a slow uplink the
        # backlog builds up on the camera system's side, not inside this proxy.
        self.sock = socket.create_server(("127.0.0.1", port))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        self.sock.setblocking(False)
        self.task = asyncio.ensure_future(self._accept())

    async def stop(self):
        self.task.cancel()
        for t in list(self.conns):
            t.cancel()
        self.sock.close()

    async def _accept(self):
        loop = asyncio.get_running_loop()
        while True:
            conn, _ = await loop.sock_accept(self.sock)
            t = asyncio.ensure_future(self._handle(conn))
            self.conns.add(t)
            t.add_done_callback(self.conns.discard)

    async def _handle(self, conn):
        loop = asyncio.get_running_loop()
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        first = await loop.sock_recv(conn, 1)
        if not first:
            conn.close()
            return
        tls = first == b"\x16"
        s_reader, s_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        s_writer.write(first)
        started = time.monotonic()
        uploaded = 0

        async def upload():
            nonlocal uploaded
            while True:
                rate = self.tls_rate if tls else self.plain_rate
                data = await loop.sock_recv(conn, 2048 if rate else 65536)
                if not data:
                    return
                s_writer.write(data)
                await s_writer.drain()
                uploaded += len(data)
                if rate:
                    await asyncio.sleep(len(data) / rate)
                if not tls and time.monotonic() - started > self.plain_reset_after and uploaded > 50000:
                    self.resets += 1
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # RST
                    return

        async def download():
            while True:
                data = await s_reader.read(65536)
                if not data:
                    return
                await loop.sock_sendall(conn, data)

        tasks = [asyncio.ensure_future(upload()), asyncio.ensure_future(download())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            s_writer.transport.abort()
            conn.close()


def api(port, method, path, body=None, cookie=""):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"X-Requested-With": "eris-cctv", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    c.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = c.getresponse()
    data = r.read()
    set_cookie = r.getheader("Set-Cookie", "")
    c.close()
    return r.status, json.loads(data), set_cookie.split(";")[0]


async def ahttp(*a, **kw):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: api(*a, **kw))


async def watch_for(port, path, cookie, seconds):
    """Watch like a browser for some seconds; returns (init received?, media fragments)."""
    ws = await connect("127.0.0.1", port, path, headers={"Cookie": cookie})
    init, frags = False, 0
    end = time.monotonic() + seconds
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.recv(), left)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes) and msg[0] == 1:
                init = True
            elif isinstance(msg, bytes) and msg[0] == 2 and init:
                frags += 1
    finally:
        await ws.close()
    return init, frags


async def relink(agent, **changes):
    """Point the agent's (only) server link somewhere else, like editing it in the panel."""
    entry = agent.cfg["servers"][0]
    entry.update(changes)
    entry["tls_pin"] = ""
    old = agent.links.pop(entry["id"], None)
    if old:
        await old.stop()
    return agent._add_link(entry)


async def wait_connected(link, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if link.state == "connected":
            return True
        await asyncio.sleep(0.2)
    return False


async def main():
    check(bool(find_ffmpeg()), "ffmpeg found")
    scfg = server_mod.default_config()
    scfg.update({"port": SERVER_PORT, "bind": "127.0.0.1"})
    scfg["camera_accounts"] = [{"username": "cam", "secret": make_secret("campass1"), "created": 0}]
    scfg["users"] = [{"username": "admin", "secret": make_secret("adminpass"), "role": "admin"}]
    save_json(config_path("server"), scfg)

    acfg = agent_mod.default_config()
    acfg["panel"].update({"port": AGENT_PORT, "bind": "127.0.0.1", "secret": make_secret("panelpass")})
    cam = agent_mod.camera_from_device(acfg, {"kind": "test", "path": "testsrc", "name": "Test cam", "index": 0},
                                       None, "Gate")
    cam.update({"width": 640, "height": 360, "fps": 10})
    acfg["cameras"] = [cam]
    acfg["servers"] = [agent_mod.new_server_entry("127.0.0.1", PROXY_PORT, "cam", "campass1", "off")]
    save_json(config_path("agent"), acfg)

    server = server_mod.Server()
    old_server = server_mod.Server()
    old_server.cfg.update({"port": OLD_SERVER_PORT})
    old_server.cfg["tls"]["mode"] = "off"
    net = ThrottlingNetwork(SERVER_PORT)
    await net.start(PROXY_PORT)
    tasks = [asyncio.ensure_future(server.run()), asyncio.ensure_future(old_server.run())]
    await asyncio.sleep(1)
    agent = agent_mod.Agent()
    at = asyncio.ensure_future(agent.run())
    _, _, cookie = await ahttp(SERVER_PORT, "POST", "/api/login", {"username": "admin", "password": "adminpass"})
    link = agent.links[acfg["servers"][0]["id"]]
    check(await wait_connected(link, 20), "agent connected through the throttling network")
    view = "/api/view/%s/%s" % (agent.agent_id, cam["id"])

    print("A: encryption off, plain uploads throttled to 5 KB/s and reset after 20 s")
    init, frags = await watch_for(SERVER_PORT, view, cookie, 26)
    print("    init=%s fragments=%d resets=%d" % (init, frags, net.resets))
    check(frags <= 4 and net.resets >= 1, "unencrypted: video barely moves and the link is reset (the old bug)")

    print("B: automatic encryption through the same network")
    link = await relink(agent, tls_mode="auto")
    check(await wait_connected(link, 20), "agent connected")
    check(link.secure, "link uses TLS")
    resets = net.resets
    init, frags = await watch_for(SERVER_PORT, view, cookie, 20)
    print("    init=%s fragments=%d resets=%d" % (init, frags, net.resets - resets))
    check(init and frags >= 14, "TLS: video flows normally (%d fragments in 20 s)" % frags)
    check(net.resets == resets and link.state == "connected", "no resets, link stays up")

    print("C: automatic encryption, server without TLS (like 1.0)")
    link = await relink(agent, tls_mode="auto", port=OLD_SERVER_PORT)
    t = time.monotonic()
    check(await wait_connected(link, 30), "agent connected after %.1fs" % (time.monotonic() - t))
    check(not link.secure and not link.server_tls, "fell back to an unencrypted link")
    _, _, ocookie = await ahttp(OLD_SERVER_PORT, "POST", "/api/login", {"username": "admin", "password": "adminpass"})
    init, frags = await watch_for(OLD_SERVER_PORT, view, ocookie, 8)
    check(init and frags >= 4, "video flows (%d fragments)" % frags)

    print("D: TLS on a link much slower than the camera (8 KB/s)")
    net.tls_rate = 8000
    link = await relink(agent, tls_mode="auto", port=PROXY_PORT)
    check(await wait_connected(link, 20), "agent connected")
    sent0 = link.bytes_sent
    init, frags = await watch_for(SERVER_PORT, view, cookie, 30)
    print("    fragments=%d skipped=%d queued=%d bytes, written to the socket %d KB, camera %s bit/s"
          % (frags, link.dropped, link._queued, (link.bytes_sent - sent0) // 1024,
             agent.cameras[cam["id"]].bitrate()))
    check(init and frags >= 2, "video keeps arriving (%d fragments)" % frags)
    check(link.dropped > 0, "old fragments skipped instead of piling up (%d)" % link.dropped)
    check(link._queued < 400000, "send queue stays short (%d bytes)" % link._queued)

    agent.stop()
    await asyncio.wait_for(at, 60)
    server._stop.set()
    old_server._stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    await net.stop()
    print("\nALL THROTTLE CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
