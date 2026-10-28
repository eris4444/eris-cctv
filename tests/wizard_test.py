"""Drives the interactive setup wizards with scripted answers (no real service is installed).

Run:  python tests/wizard_test.py      (needs ffmpeg with libx264)
"""
import asyncio
import builtins
import getpass
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="eriscctv-wizard-")
os.environ["ERIS_CCTV_HOME"] = HOME
os.environ["ERIS_CCTV_NO_SERVICE"] = "1"

from eriscctv import cli, devices  # noqa: E402
from eriscctv import server as server_mod  # noqa: E402
from eriscctv.util import config_path, load_json  # noqa: E402


class Script:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("no scripted answer for prompt: %r" % prompt)
        a = self.answers.pop(0)
        print(prompt + ("***" if "assword" in prompt else a))
        return a


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


def run_server_in_thread():
    srv = server_mod.Server()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=lambda: loop.run_until_complete(srv.run()), daemon=True)
    t.start()
    return srv, loop


def main():
    # never ask the internet for the public IP in tests
    cli.public_ip = lambda: "127.0.0.1"
    print("== server wizard ==")
    s = Script(["18877",          # port
                "camuser",        # camera account
                "camsecret1", "camsecret1",
                "n",              # different panel login
                "boss", "bosspass1", "bosspass1",
                "",               # public address (default 127.0.0.1)
                "1"])             # TLS: automatic self-signed certificate
    builtins.input = s
    getpass.getpass = s
    cli.setup_server()
    cfg = load_json(config_path("server"))
    check(cfg["tls"]["mode"] == "auto", "TLS mode automatic")
    check(os.path.isfile(os.path.join(HOME, "config", "tls", "cert.pem")), "self-signed certificate created")
    check(cfg["port"] == 18877, "server port saved")
    check(cfg["camera_accounts"][0]["username"] == "camuser", "camera account saved")
    check(cfg["users"][0]["username"] == "boss" and cfg["users"][0]["role"] == "admin", "panel admin saved")
    check("bosspass1" not in open(config_path("server")).read(), "no plain password in server config")

    srv, loop = run_server_in_thread()
    import time
    time.sleep(1)

    print("== camera wizard ==")
    real_list = devices.list_devices
    devices.list_devices = lambda ff, include_test=False: real_list(ff, True)  # add the test pattern
    devs = devices.list_devices(cli.find_ffmpeg(), True)
    test_index = [i for i, v in enumerate(devs["video"], 1) if v["kind"] == "test"][0]
    s = Script([str(test_index),   # choose the test pattern camera
                "y",               # send its microphone (test tone)
                "Porch",           # camera name
                "My PC",           # system name
                "18091",           # panel port
                "admin",           # panel user
                "panelpass", "panelpass",
                "n",               # LAN access: no
                "y",               # add a server now
                "127.0.0.1:18877",  # address with port
                "camuser", "wrongpass",  # user, password (wrong)
                "1",               # try again
                "127.0.0.1", "18877", "camuser", "camsecret1",
                "n",               # add another server
                "n"])              # recording off
    builtins.input = s
    getpass.getpass = s
    cli.setup_camera()
    cfg = load_json(config_path("agent"))
    cam = cfg["cameras"][0]
    check(cam["name"] == "Porch", "camera name saved")
    check(cam["device"]["kind"] == "test", "chosen webcam saved")
    check(cam["audio"] and cam["audio"]["kind"] == "test", "microphone enabled")
    check(cfg["panel"]["port"] == 18091 and cfg["panel"]["bind"] == "127.0.0.1", "panel port + local-only access")
    check(len(cfg["servers"]) == 1 and cfg["servers"][0]["password"] == "camsecret1",
          "server saved after the retry with the right password")
    from eriscctv.certs import default_paths, fingerprint_file
    check(cfg["servers"][0]["tls_mode"] == "auto", "link encryption automatic")
    check(cfg["servers"][0]["tls_pin"] == fingerprint_file(default_paths()[0]),
          "the connection test used TLS and pinned the server certificate")
    check(any("Try again" in p or "Choose" in p for p in s.prompts), "wrong password offered a retry")
    check(cfg["storage"]["enabled"] is False, "recording left off")

    print("== 1.0 config migration ==")
    from eriscctv import agent as agent_mod
    from eriscctv.util import save_json
    old_srv = os.path.join(HOME, "old-server.json")
    save_json(old_srv, {"port": 8800, "tls": {"enabled": False, "cert": "", "key": ""}})
    check(server_mod.load_config(old_srv)["tls"]["mode"] == "auto", "server without TLS -> automatic TLS")
    save_json(old_srv, {"port": 8800, "tls": {"enabled": True, "cert": "/x/c.pem", "key": "/x/k.pem"}})
    check(server_mod.load_config(old_srv)["tls"] == {"mode": "files", "cert": "/x/c.pem", "key": "/x/k.pem"},
          "server with its own certificate keeps it")
    old_agent = os.path.join(HOME, "old-agent.json")
    save_json(old_agent, {"agent_id": "abcd1234", "servers": [
        {"id": "s1", "address": "a", "port": 1, "username": "u", "password": "p", "tls": False},
        {"id": "s2", "address": "b", "port": 2, "username": "u", "password": "p", "tls": True, "tls_pin": "ff"}]})
    servers = agent_mod.load_config(old_agent)["servers"]
    check([s["tls_mode"] for s in servers] == ["auto", "on"] and "tls" not in servers[0],
          "camera links: tls false -> automatic, tls true -> always")

    print("== number parsing ==")
    check(cli.parse_numbers("1,3", 3) == [1, 3], "1,3")
    check(cli.parse_numbers(" 2 - 3 ", 3) == [2, 3], "range with spaces tolerated")
    check(cli.parse_numbers("3, 1", 3) == [1, 3], "comma + space")
    check(cli.parse_numbers("all", 3) == [1, 2, 3], "all")
    try:
        cli.parse_numbers("4", 3)
        raise AssertionError("4 accepted")
    except ValueError:
        print("  ok - out of range rejected")
    loop.call_soon_threadsafe(srv._stop.set)
    print("\nALL WIZARD CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
