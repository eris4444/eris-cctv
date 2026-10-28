"""Checks a real installation (systemd services) from the outside, like a browser would.

usage: python3 tests/ci_check.py --server https://127.0.0.1:8800 --user admin --password X
                                 --agent http://127.0.0.1:8090 --agent-password Y
                                 [--cameras 1] [--audio] [--recordings]
"""
import argparse
import asyncio
import json
import os
import ssl
import sys
import time
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eriscctv.mp4 import codec_info  # noqa: E402
from eriscctv.ws import connect  # noqa: E402

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def check(cond, msg):
    if not cond:
        print("FAIL -", msg)
        sys.exit(1)
    print("  ok -", msg)


def call(base, method, path, body=None, cookie=""):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Requested-With": "eris-cctv", "Content-Type": "application/json",
                                          "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}"), (r.headers.get("Set-Cookie") or "").split(";")[0]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), ""


def login(base, user, pw):
    for _ in range(30):
        try:
            st, body, cookie = call(base, "POST", "/api/login", {"username": user, "password": pw})
            if st == 200:
                return cookie
            print("   login answered", st, body)
        except OSError as e:
            print("   waiting for", base, e)
        time.sleep(2)
    check(False, "login to %s" % base)


async def watch(base, path, cookie, frags_wanted=4):
    u = urlsplit(base)
    ctx = CTX if u.scheme == "https" else None
    ws = await connect(u.hostname, u.port, path, ssl_ctx=ctx, headers={"Cookie": cookie})
    init, frags, metas = None, 0, []
    end = time.time() + 60
    try:
        while time.time() < end and frags < frags_wanted:
            m = await asyncio.wait_for(ws.recv(), 60)
            if isinstance(m, str):
                metas.append(json.loads(m))
            elif m[0] == 1:
                init = m[1:]
            elif m[0] == 2 and init:
                frags += 1
    finally:
        await ws.close()
    return init, frags, metas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password")
    ap.add_argument("--agent")
    ap.add_argument("--agent-user", default="admin")
    ap.add_argument("--agent-password")
    ap.add_argument("--cameras", type=int, default=1)
    ap.add_argument("--audio", action="store_true")
    ap.add_argument("--recordings", action="store_true")
    ap.add_argument("--secure", action="store_true", help="the camera -> server links must use TLS")
    a = ap.parse_args()

    if a.agent:
        print("camera panel", a.agent)
        cookie = login(a.agent, a.agent_user, a.agent_password)
        for _ in range(40):
            st, body, _ = call(a.agent, "GET", "/api/status", cookie=cookie)
            if sum(1 for c in body["cameras"] if c["online"]) >= a.cameras and \
                    all(s["state"] == "connected" for s in body["servers"]):
                break
            time.sleep(2)
        print("   ", json.dumps([{k: c.get(k) for k in ("name", "state", "error", "codec")} for c in body["cameras"]]))
        print("   ", json.dumps([{k: s.get(k) for k in ("address", "state", "error")} for s in body["servers"]]))
        check(sum(1 for c in body["cameras"] if c["online"]) >= a.cameras, "%d camera(s) live on the camera system" % a.cameras)
        check(all(s["state"] == "connected" for s in body["servers"]), "camera system connected to its server(s)")
        if a.secure:
            check(all(s["secure"] and s["tls_pin"] for s in body["servers"]), "links encrypted (TLS, certificate pinned)")
        cam = body["cameras"][0]
        init, frags, _ = asyncio.run(watch(a.agent, "/api/live/" + cam["id"], cookie))
        check(init and frags >= 3, "local live view streams (%d fragments)" % frags)
        if a.audio:
            check(codec_info(init)["audio"], "audio track present in local stream")
        if a.recordings:
            time.sleep(5)
            st, rec, _ = call(a.agent, "GET", "/api/recordings?camera=" + cam["id"], cookie=cookie)
            check(rec["days"], "recording folder created")
            st, files, _ = call(a.agent, "GET", "/api/recordings?camera=%s&day=%s" % (cam["id"], rec["days"][0]),
                                cookie=cookie)
            check(files["files"] and files["files"][0]["size"] > 0, "recording file is growing (%d bytes)"
                  % files["files"][0]["size"])
            st, d, _ = call(a.agent, "GET", "/api/storage/drives", cookie=cookie)
            check(d["drives"], "drives listed: " + ", ".join(x["mount"] or x["device"] for x in d["drives"]))

    if a.server:
        print("server panel", a.server)
        cookie = login(a.server, a.user, a.password)
        for _ in range(30):
            st, body, _ = call(a.server, "GET", "/api/cameras", cookie=cookie)
            if sum(1 for c in body["cameras"] if c["online"]) >= a.cameras:
                break
            time.sleep(2)
        check(sum(1 for c in body["cameras"] if c["online"]) >= a.cameras, "%d camera(s) online on the server"
              % a.cameras)
        for c in [c for c in body["cameras"] if c["online"]][:a.cameras]:
            init, frags, metas = asyncio.run(watch(a.server, "/api/view/" + c["key"], cookie))
            info = codec_info(init or b"")
            check(init and frags >= 3, "%s streams through the server (%s, %d fragments)" % (c["name"], info["codec"], frags))
            if a.audio:
                check(info["audio"], "%s has audio" % c["name"])
        if a.secure:
            st, ag, _ = call(a.server, "GET", "/api/agents", cookie=cookie)
            check(all(x["secure"] for x in ag["agents"] if x["online"]), "server sees encrypted camera links")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
