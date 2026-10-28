"""Development runner: a server (port 18800) and a camera agent (port 18090) with test-pattern cameras.

Logins: server admin / adminpass, camera panel admin / panelpass. Data lives in ./.devhome
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ERIS_CCTV_HOME", os.path.join(ROOT, ".devhome"))

from eriscctv import agent as agent_mod  # noqa: E402
from eriscctv import server as server_mod  # noqa: E402
from eriscctv.auth import make_secret  # noqa: E402
from eriscctv.util import config_path, save_json, setup_logging  # noqa: E402

TEST_VIDEO = {"kind": "test", "path": "testsrc", "name": "Test pattern (virtual camera)", "index": 0}
TEST_AUDIO = {"kind": "test", "path": "sine", "name": "Test tone (440 Hz)", "index": 0}


def prepare():
    if not os.path.exists(config_path("server")):
        s = server_mod.default_config()
        s.update({"port": 18800, "bind": "127.0.0.1", "public_address": "127.0.0.1"})
        s["camera_accounts"] = [{"username": "cam", "secret": make_secret("campass1"), "created": 0, "note": "dev"}]
        s["users"] = [{"username": "admin", "secret": make_secret("adminpass"), "role": "admin"}]
        save_json(config_path("server"), s)
    if not os.path.exists(config_path("agent")):
        a = agent_mod.default_config()
        a["name"] = "Dev camera PC"
        a["panel"].update({"port": 18090, "bind": "127.0.0.1", "secret": make_secret("panelpass")})
        c1 = agent_mod.camera_from_device(a, TEST_VIDEO, TEST_AUDIO, "Front door")
        c1.update({"width": 1280, "height": 720, "fps": 15})
        a["cameras"].append(c1)
        c2 = agent_mod.camera_from_device(a, TEST_VIDEO, None, "Garage")
        c2.update({"width": 640, "height": 360, "fps": 10, "device": dict(TEST_VIDEO, path="testsrc2")})
        a["cameras"].append(c2)
        a["servers"].append(agent_mod.new_server_entry("127.0.0.1", 18800, "cam", "campass1"))
        save_json(config_path("agent"), a)


async def main(which):
    tasks = []
    if which in ("both", "server"):
        tasks.append(server_mod.Server().run())
    if which in ("both", "agent"):
        tasks.append(agent_mod.Agent().run())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--ffmpeg" in args:
        i = args.index("--ffmpeg")
        os.environ["ERIS_CCTV_FFMPEG"] = args[i + 1]
        del args[i:i + 2]
    prepare()
    setup_logging("dev")
    try:
        asyncio.run(main(args[0] if args else "both"))
    except KeyboardInterrupt:
        pass
