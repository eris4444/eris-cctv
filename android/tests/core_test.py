"""Tests the Android app's core (Kotlin, run on the JVM) against the real Eris CCTV server.

1. fMP4 written by Mp4Muxer from ffmpeg-made H.264 + AAC decodes cleanly, with rotation metadata
2. login: right password (TLS, pinned), wrong password, encryption off
3. streaming through the server: codec, keyframes, small fragments, encrypted link

Run:  python android/tests/core_test.py      (needs ffmpeg with libx264 and a JDK)
"""
import asyncio
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ANDROID = os.path.dirname(HERE)
ROOT = os.path.dirname(ANDROID)
sys.path.insert(0, ROOT)
sys.path.insert(0, ANDROID)
HOME = tempfile.mkdtemp(prefix="eriscctv-android-")
os.environ["ERIS_CCTV_HOME"] = HOME

import build  # noqa: E402
from eriscctv import server as server_mod  # noqa: E402
from eriscctv.auth import make_secret  # noqa: E402
from eriscctv.mp4 import KeyframeDetector, Mp4Splitter, codec_info  # noqa: E402
from eriscctv.util import config_path, find_ffmpeg, save_json  # noqa: E402
from eriscctv.ws import connect  # noqa: E402

PORT = 18970
FPS = 15


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


def jvm(jar, *args, timeout=120):
    cp = os.pathsep.join([jar, build.kotlin_stdlib()])
    r = subprocess.run([build.java(), "-cp", cp, "ir.eris.cctv.coretest.CoreTestKt"] + [str(a) for a in args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        print(r.stderr[-3000:])
        raise AssertionError("JVM tool failed: %s" % args[0])
    return json.loads(r.stdout.strip().splitlines()[-1])


async def ajvm(*a, **kw):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: jvm(*a, **kw))


def api(method, path, body=None, cookie=""):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    headers = {"X-Requested-With": "eris-cctv", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    c.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = c.getresponse()
    data = r.read()
    ck = (r.getheader("Set-Cookie") or "").split(";")[0]
    c.close()
    return r.status, json.loads(data), ck


async def ahttp(*a, **kw):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: api(*a, **kw))


def make_inputs(ffmpeg):
    h264, aac = os.path.join(HOME, "in.h264"), os.path.join(HOME, "in.aac")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=%d" % FPS, "-t", "14", "-c:v", "libx264", "-profile:v", "baseline",
                    "-bf", "0", "-g", str(FPS), "-x264-params", "aud=1:keyint=%d:min-keyint=%d:scenecut=0" % (FPS, FPS),
                    "-pix_fmt", "yuv420p", "-f", "h264", h264], check=True)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000", "-t", "14", "-ac", "1", "-c:a", "aac", "-b:a", "64k",
                    "-f", "adts", aac], check=True)
    return h264, aac


def probe(ffmpeg, path):
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path, "-f", "null", "-"], capture_output=True, text=True)
    frames = re.findall(r"frame=\s*(\d+)", r.stderr)
    return r.returncode, r.stderr, int(frames[-1]) if frames else 0


async def main():
    ffmpeg = find_ffmpeg()
    check(bool(ffmpeg), "ffmpeg: %s" % ffmpeg)
    jar = build.build_core() if not os.environ.get("CORE_JAR") else os.environ["CORE_JAR"]
    h264, aac = make_inputs(ffmpeg)

    print("Mp4Muxer output")
    out = os.path.join(HOME, "mux.mp4")
    st = await ajvm(jar, "mux", h264, aac, out, FPS, 0)
    code, err, frames = probe(ffmpeg, out)
    check(code == 0 and "error" not in err.lower(), "fMP4 decodes without errors")
    check(frames >= 14 * FPS - 3, "%d video frames decoded (14 s at %d fps)" % (frames, FPS))
    check("h264 (Constrained Baseline)" in err or "h264" in err, "H.264 track")
    check("aac" in err and "48000 Hz" in err, "AAC 48 kHz track")
    check(st["keys"] in range(13, 16) and st["fragments"] >= 14 * FPS // 2 - 2,
          "%d fragments, %d keyframe fragments" % (st["fragments"], st["keys"]))
    data = open(out, "rb").read()
    sp = Mp4Splitter()
    items = sp.feed(data)
    init = items[0][1]
    info = codec_info(init)
    check(items[0][0] == "init" and info["width"] == 640 and info["height"] == 360,
          "init segment: %s %dx%d" % (info["codec"], info["width"], info["height"]))
    check(info["codec"] == st["codec"], "codec string matches the server's reading (%s)" % st["codec"])
    det = KeyframeDetector(init)
    flags = [det.is_key(d) for k, d in items[1:]]
    check(flags[0] and sum(flags) == st["keys"], "server's KeyframeDetector agrees (%d keyframe fragments)" % sum(flags))
    rot = os.path.join(HOME, "rot.mp4")
    await ajvm(jar, "mux", h264, "-", rot, FPS, 90)
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", rot], capture_output=True, text=True)
    check(re.search(r"rotation of -?90", r.stderr) is not None, "rotation 90 stored as display matrix")

    print("login against the server")
    scfg = server_mod.default_config()
    scfg.update({"port": PORT, "bind": "127.0.0.1"})
    scfg["camera_accounts"] = [{"username": "cam", "secret": make_secret("campass1"), "created": 0}]
    scfg["users"] = [{"username": "admin", "secret": make_secret("adminpass"), "role": "admin"}]
    save_json(config_path("server"), scfg)
    server = server_mod.Server()
    task = asyncio.ensure_future(server.run())
    await asyncio.sleep(1.5)
    r = await ajvm(jar, "test", "127.0.0.1", PORT, "cam", "campass1", "auto")
    check(r["ok"] and r["secure"] and len(r["fingerprint"]) == 64, "right password, TLS: %s" % r)
    r = await ajvm(jar, "test", "127.0.0.1", PORT, "cam", "wrong!!", "auto")
    check(not r["ok"] and "password" in r["error"].lower(), "wrong password refused: %s" % r["error"])
    r = await ajvm(jar, "test", "127.0.0.1", PORT, "cam", "campass1", "off")
    check(r["ok"] and not r["secure"] and r["server_tls"], "encryption off still logs in (unencrypted)")

    print("streaming through the server like the phone")
    stream = asyncio.ensure_future(ajvm(jar, "stream", "127.0.0.1", PORT, "cam", "campass1", "auto", h264, aac,
                                        os.path.join(HOME, "stream.mp4"), 12, FPS, 0, timeout=90))
    _, _, cookie = await ahttp("POST", "/api/login", {"username": "admin", "password": "adminpass"})
    key = None
    for _ in range(60):
        _, body, _ = await ahttp("GET", "/api/cameras", cookie=cookie)
        cams = [c for c in body["cameras"] if c["online"]]
        if cams:
            key = cams[0]["key"]
            break
        await asyncio.sleep(0.5)
    check(key is not None, "phone camera online on the server (%s)" % key)
    ws = await connect("127.0.0.1", PORT, "/api/view/" + key, headers={"Cookie": cookie})
    init, kinds, t0 = None, [], time.time()
    while time.time() - t0 < 6:
        try:
            m = await asyncio.wait_for(ws.recv(), 3)
        except asyncio.TimeoutError:
            break
        if isinstance(m, bytes) and m[0] == 1:
            init = m[1:]
        elif isinstance(m, bytes) and m[0] in (2, 3) and init:
            kinds.append((m[0], time.time(), m[1:]))
    await ws.close()
    info = codec_info(init or b"")
    check(init is not None and info["video"].startswith("avc1.42") and info["audio"] == "mp4a.40.2",
          "viewer got the init segment: %s" % info["codec"])
    check(kinds and kinds[0][0] == 3, "viewer starts at a keyframe")
    span = kinds[-1][1] - kinds[0][1] if len(kinds) > 1 else 0
    check(span > 0 and (len(kinds) - 1) / span >= 5, "small fragments: %.1f per second" % ((len(kinds) - 1) / max(span, 1e-3)))
    viewed = os.path.join(HOME, "viewed.mp4")
    with open(viewed, "wb") as f:
        f.write(init)
        for _k, _t, d in kinds:
            f.write(d)
    code, err, frames = probe(ffmpeg, viewed)
    check(code == 0 and frames > 50, "what the viewer received decodes (%d frames)" % frames)
    _, ag, _ = await ahttp("GET", "/api/agents", cookie=cookie)
    phone = [a for a in ag["agents"] if a["name"] == "JVM phone test"]
    check(phone and phone[0]["secure"], "server sees the phone's link as encrypted")
    st = await stream
    check(st["state"] == "connected" and st["secure"] and st["pin"] and st["subscribes"] >= 1,
          "phone side: connected, TLS, pinned, %d subscription(s), %d bytes sent" % (st["subscribes"], st["bytes_sent"]))
    server._stop.set()
    await asyncio.wait_for(task, 10)
    print("\nALL ANDROID CORE CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
