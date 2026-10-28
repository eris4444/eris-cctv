"""End-to-end test: server + camera agent (test pattern) + browser-like viewers, all in one process.

Run:  python tests/e2e_test.py            (needs ffmpeg with libx264 on PATH or ERIS_CCTV_FFMPEG)
"""
import asyncio
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HOME = tempfile.mkdtemp(prefix="eriscctv-e2e-")
os.environ["ERIS_CCTV_HOME"] = HOME

from eriscctv import agent as agent_mod  # noqa: E402
from eriscctv import server as server_mod  # noqa: E402
from eriscctv.auth import make_secret  # noqa: E402
from eriscctv.mp4 import codec_info  # noqa: E402
from eriscctv.util import config_path, find_ffmpeg, save_json  # noqa: E402
from eriscctv.ws import connect  # noqa: E402

SERVER_PORT = int(os.environ.get("E2E_SERVER_PORT", "18850"))
AGENT_PORT = int(os.environ.get("E2E_AGENT_PORT", "18095"))
WITH_AUDIO = os.environ.get("E2E_AUDIO", "1") == "1"


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


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
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = data
    return r.status, parsed, set_cookie.split(";")[0]


async def ahttp(*a, **kw):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: api(*a, **kw))


async def watch(port, path, cookie, want_frags=3, timeout=40):
    ws = await connect("127.0.0.1", port, path, headers={"Cookie": cookie})
    metas, init, frags = [], None, 0
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and frags < want_frags:
            msg = await asyncio.wait_for(ws.recv(), max(1, deadline - time.time()))
            if isinstance(msg, str):
                metas.append(json.loads(msg))
            elif msg[0] == 1:
                init = msg[1:]
            elif msg[0] == 2 and init is not None:
                frags += 1
    finally:
        await ws.close()
    return metas, init, frags


async def main():
    ffmpeg = find_ffmpeg()
    check(bool(ffmpeg), "ffmpeg found: %s" % ffmpeg)

    scfg = server_mod.default_config()
    scfg["port"] = SERVER_PORT
    scfg["bind"] = "127.0.0.1"
    scfg["camera_accounts"] = [{"username": "cam", "secret": make_secret("campass1"), "created": 0}]
    scfg["users"] = [{"username": "admin", "secret": make_secret("adminpass"), "role": "admin"},
                     {"username": "guest", "secret": make_secret("guestpass"), "role": "viewer", "cameras": []}]
    save_json(config_path("server"), scfg)

    rec_dir = os.path.join(HOME, "recordings")
    acfg = agent_mod.default_config()
    acfg["panel"].update({"port": AGENT_PORT, "bind": "127.0.0.1", "secret": make_secret("panelpass")})
    test_audio = {"kind": "test", "path": "sine", "name": "Test tone", "index": 0} if WITH_AUDIO else None
    cam = agent_mod.camera_from_device(acfg, {"kind": "test", "path": "testsrc", "name": "Test cam", "index": 0},
                                       test_audio, "Front door")
    cam.update({"width": 640, "height": 360, "fps": 10})
    acfg["cameras"] = [cam]
    acfg["servers"] = [agent_mod.new_server_entry("127.0.0.1", SERVER_PORT, "cam", "campass1"),
                       agent_mod.new_server_entry("127.0.0.1", SERVER_PORT + 1, "cam", "x")]  # 2nd: unreachable
    acfg["storage"] = {"enabled": True, "path": rec_dir, "mount": "", "segment_minutes": 10}
    save_json(config_path("agent"), acfg)

    server = server_mod.Server()
    agent = agent_mod.Agent()
    st = asyncio.ensure_future(server.run())
    at = asyncio.ensure_future(agent.run())
    await asyncio.sleep(1.5)

    print("server panel")
    status, body, _ = await ahttp(SERVER_PORT, "POST", "/api/login", {"username": "admin", "password": "bad"})
    check(status == 401, "wrong password refused (%s)" % status)
    status, body, cookie = await ahttp(SERVER_PORT, "POST", "/api/login",
                                       {"username": "admin", "password": "adminpass"})
    check(status == 200 and cookie, "admin login works")
    status, body, _ = await ahttp(SERVER_PORT, "POST", "/api/logout", {}, cookie="ecctv_srv=x")
    status, body, _ = await ahttp(SERVER_PORT, "GET", "/api/cameras")
    check(status == 401, "camera list needs login")

    key = None
    for _ in range(40):
        status, body, _ = await ahttp(SERVER_PORT, "GET", "/api/cameras", cookie=cookie)
        cams = body.get("cameras", [])
        if cams and cams[0]["online"]:
            key = cams[0]["key"]
            break
        await asyncio.sleep(0.5)
    check(key is not None, "camera appears online on the server (%s)" % key)
    check(cams[0]["name"] == "Front door", "camera name reaches the server")

    print("live view through the server")
    agent_id, cam_id = key.split("/")
    metas, init, frags = await watch(SERVER_PORT, "/api/view/%s/%s" % (agent_id, cam_id), cookie)
    check(init is not None, "init segment received (%d bytes)" % len(init or b""))
    info = codec_info(init)
    check(info["video"].startswith("avc1."), "video codec %s" % info["video"])
    if WITH_AUDIO:
        check(info["audio"] == "mp4a.40.2", "audio track present (%s)" % info["audio"])
    check(frags >= 3, "%d media fragments received" % frags)
    check(any(m.get("online") for m in metas), "meta says online")

    print("permissions")
    status, body, gcookie = await ahttp(SERVER_PORT, "POST", "/api/login",
                                        {"username": "guest", "password": "guestpass"})
    status, body, _ = await ahttp(SERVER_PORT, "GET", "/api/cameras", cookie=gcookie)
    check(body["cameras"] == [], "viewer without cameras sees none")
    status, body, _ = await ahttp(SERVER_PORT, "GET", "/api/users", cookie=gcookie)
    check(status == 403, "viewer cannot manage users")
    status, body, _ = await ahttp(SERVER_PORT, "POST", "/api/users", {"username": "x", "password": "123456"},
                                  cookie=gcookie)
    check(status == 403, "viewer cannot add users")
    def no_csrf():
        c = http.client.HTTPConnection("127.0.0.1", SERVER_PORT, timeout=10)
        c.request("POST", "/api/accounts", "{}", {"Cookie": cookie, "Content-Type": "application/json"})
        code = c.getresponse().status
        c.close()
        return code
    code = await asyncio.get_running_loop().run_in_executor(None, no_csrf)
    check(code == 403, "POST without CSRF header refused")

    print("agent panel")
    status, body, acookie = await ahttp(AGENT_PORT, "POST", "/api/login",
                                        {"username": "admin", "password": "panelpass"})
    check(status == 200, "agent panel login works")
    status, body, _ = await ahttp(AGENT_PORT, "GET", "/api/status", cookie=acookie)
    srv = {s["port"]: s for s in body["servers"]}
    check(srv[SERVER_PORT]["state"] == "connected", "agent shows server connected")
    check(srv[SERVER_PORT + 1]["state"] in ("error", "connecting"), "unreachable server reported: %s"
          % srv[SERVER_PORT + 1]["error"])
    check(body["storage"]["state"] == "ok", "storage ok: %s" % body["storage"]["message"])
    metas, init, frags = await watch(AGENT_PORT, "/api/live/%s" % cam_id, acookie)
    check(init is not None and frags >= 3, "local live view works (%d fragments)" % frags)
    status, body, _ = await ahttp(AGENT_PORT, "POST", "/api/servers/test",
                                  {"address": "127.0.0.1", "port": SERVER_PORT, "username": "cam",
                                   "password": "wrong!"}, cookie=acookie)
    check(body.get("ok") is False and "password" in body.get("error", "").lower(),
          "server test with wrong password: %s" % body.get("error"))
    status, body, _ = await ahttp(AGENT_PORT, "POST", "/api/servers/test",
                                  {"address": "127.0.0.1", "port": SERVER_PORT, "username": "cam",
                                   "password": "campass1"}, cookie=acookie)
    check(body.get("ok") is True, "server test with right password: %s" % body)
    status, body, _ = await ahttp(AGENT_PORT, "GET", "/api/storage/drives", cookie=acookie)
    check(status == 200 and body["drives"], "%d drive(s) listed" % len(body["drives"]))

    print("server sees agent")
    status, body, _ = await ahttp(SERVER_PORT, "GET", "/api/agents", cookie=cookie)
    check(body["agents"][0]["online"], "agent listed as online")

    print("camera account removal disconnects the agent")
    status, body, _ = await ahttp(SERVER_PORT, "POST", "/api/accounts", {"username": "cam2", "password": "abcdef"},
                                  cookie=cookie)
    check(status == 200, "second camera account added")

    print("recording")
    await asyncio.sleep(3)
    status, body, _ = await ahttp(AGENT_PORT, "GET", "/api/recordings?camera=%s" % cam_id, cookie=acookie)
    check(body["days"], "recording day folder exists")
    agent.stop()
    await asyncio.wait_for(at, 60)
    files = []
    for dp, _d, fs in os.walk(rec_dir):
        files += [os.path.join(dp, f) for f in fs if f.endswith(".mp4")]
    check(files, "finalized recording: %s" % [os.path.basename(f) for f in files])
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", files[0], "-f", "null", "-"], capture_output=True,
                           text=True)
    check(probe.returncode == 0, "recording decodes cleanly with ffmpeg")
    m = [ln for ln in probe.stderr.splitlines() if "Duration" in ln]
    print("   ", m[0].strip() if m else "")

    await asyncio.sleep(1)
    status, body, _ = await ahttp(SERVER_PORT, "GET", "/api/cameras", cookie=cookie)
    check(body["cameras"] and not body["cameras"][0]["online"], "camera shows offline after agent stops")
    server._stop.set()
    await asyncio.wait_for(st, 10)
    print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
