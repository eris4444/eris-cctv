"""eris-cctv command: interactive menu, setup wizards and service commands."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

from . import APP_NAME, REPO, __version__, devices, service
from .auth import make_secret
from .util import (IS_LINUX, IS_WINDOWS, app_root, config_path, dirs, ffmpeg_features, find_ffmpeg, h264_encoder,
                   human_bytes, is_root, load_json, local_ips, log_tail, save_json, setup_logging)

USE_COLOR = sys.stdout is not None and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if IS_WINDOWS and USE_COLOR:
    os.system("")  # enables ANSI colors in the Windows console


def c(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else str(text)


def bold(t):
    return c(t, "1")


def green(t):
    return c(t, "32")


def red(t):
    return c(t, "31")


def yellow(t):
    return c(t, "33")


def cyan(t):
    return c(t, "36")


def dim(t):
    return c(t, "2")


def title(text):
    print()
    print(bold(cyan("== %s ==" % text)))


def ok(text):
    print(green("  [ok] ") + text)


def warn(text):
    print(yellow("  [!] ") + text)


def fail(text):
    print(red("  [x] ") + text)


class Abort(Exception):
    pass


def ask(prompt, default=None, validate=None, allow_empty=False):
    while True:
        suffix = " [%s]" % default if default not in (None, "") else ""
        try:
            value = input("  %s%s: " % (prompt, suffix)).strip()
        except EOFError:
            raise Abort()
        if not value and default not in (None, ""):
            value = str(default)
        if not value and not allow_empty:
            print(red("    Please enter a value."))
            continue
        if validate:
            err = validate(value)
            if err:
                print(red("    " + err))
                continue
        return value


def ask_yes(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            v = input("  %s [%s]: " % (prompt, hint)).strip().lower()
        except EOFError:
            raise Abort()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        print(red("    Answer y or n."))


def ask_password(prompt, confirm=True, keep_hint=False, min_len=6):
    while True:
        try:
            p1 = getpass.getpass("  %s%s: " % (prompt, " (Enter = keep current)" if keep_hint else ""))
        except EOFError:
            raise Abort()
        if not p1 and keep_hint:
            return ""
        if len(p1) < min_len:
            print(red("    Use at least %d characters." % min_len))
            continue
        if confirm:
            p2 = getpass.getpass("  Repeat password: ")
            if p1 != p2:
                print(red("    The passwords do not match."))
                continue
        return p1


def parse_numbers(text, count):
    """'1,3' / '1-3' / 'all' -> sorted list of 1-based indexes; raises ValueError."""
    text = re.sub(r"\s*-\s*", "-", text.strip().lower())
    if text in ("all", "*", "a"):
        return list(range(1, count + 1))
    out = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                rng = range(int(a), int(b) + 1)
            else:
                rng = [int(part)]
        except ValueError:
            raise ValueError("Use numbers like 1,2 or 1-3")
        for n in rng:
            if n < 1 or n > count:
                raise ValueError("%d is not in the list" % n)
            if n not in out:
                out.append(n)
    if not out:
        raise ValueError("choose at least one")
    return sorted(out)


def valid_port(v):
    return None if v.isdigit() and 1 <= int(v) <= 65535 else "Enter a port number between 1 and 65535."


def valid_user(v):
    return None if re.match(r"^[A-Za-z0-9_.@-]{1,40}$", v) else "Use letters, digits and . _ - @ only."


def pause():
    try:
        input(dim("\n  Press Enter to continue..."))
    except EOFError:
        pass


# ------------------------------------------------------------------------------ downloads
def fetch(url, timeout=30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "eris-cctv/" + __version__})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(url, dest, label):
    req = urllib.request.Request(url, headers={"User-Agent": "eris-cctv/" + __version__})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if time.time() - last > 0.5:
                last = time.time()
                pct = " %3d%%" % (got * 100 // total) if total else ""
                sys.stdout.write("\r  Downloading %s:%s %s   " % (label, pct, human_bytes(got)))
                sys.stdout.flush()
    sys.stdout.write("\r  Downloading %s: done (%s)            \n" % (label, human_bytes(got)))


def public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            ip = fetch(url, 6).decode().strip()
            if re.match(r"^[0-9a-fA-F.:]{3,45}$", ip):
                return ip
        except Exception:  # noqa: BLE001 - any failure: try the next service
            continue
    ips = local_ips()
    return ips[0] if ips else ""


# ------------------------------------------------------------------------------ ffmpeg
BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"


def ensure_ffmpeg(interactive=True) -> str:
    ff = find_ffmpeg()
    if ff and h264_encoder(ff):
        ok("ffmpeg found: %s" % ff)
        return ff
    if ff:
        warn("ffmpeg at %s has no H.264 encoder (libx264)." % ff)
    if IS_LINUX and not ff:
        pm = package_manager()
        if pm:
            print("  Installing ffmpeg with %s..." % pm[0])
            if pkg_install(pm, ["ffmpeg"]):
                ff = find_ffmpeg()
                if ff and h264_encoder(ff):
                    ok("ffmpeg installed: %s" % ff)
                    return ff
    machine = platform.machine().lower()
    if IS_WINDOWS:
        name = "ffmpeg-master-latest-win64-gpl.zip"
    elif IS_LINUX and machine in ("x86_64", "amd64"):
        name = "ffmpeg-master-latest-linux64-gpl.tar.xz"
    elif IS_LINUX and machine in ("aarch64", "arm64"):
        name = "ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
    else:
        fail("No ffmpeg with H.264 found. Install ffmpeg (with libx264) and run this again.")
        return ""
    if interactive and not ask_yes("Download a ready-made ffmpeg build from GitHub (about 100-200 MB)?", True):
        return ""
    target = os.path.join(app_root(), "ffmpeg")
    tmp = tempfile.mkdtemp(prefix="eriscctv-ffmpeg-")
    try:
        archive = os.path.join(tmp, name)
        download(BTBN + name, archive, "ffmpeg")
        print("  Extracting...")
        exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                members = [m for m in z.namelist() if m.endswith("/bin/" + exe) or m.endswith("/bin/ffprobe" +
                                                                                            (".exe" if IS_WINDOWS else ""))]
                os.makedirs(os.path.join(target, "bin"), exist_ok=True)
                for m in members:
                    with z.open(m) as src, open(os.path.join(target, "bin", os.path.basename(m)), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(archive, "r:xz") as t:
                os.makedirs(os.path.join(target, "bin"), exist_ok=True)
                for m in t.getmembers():
                    if m.isfile() and re.search(r"/bin/(ffmpeg|ffprobe)$", m.name):
                        src = t.extractfile(m)
                        dst_path = os.path.join(target, "bin", os.path.basename(m.name))
                        with open(dst_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        os.chmod(dst_path, 0o755)
    except Exception as e:  # noqa: BLE001 - shown to the user
        fail("Download failed: %s" % e)
        return ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    from . import util
    util._encoder_cache.clear()
    ff = find_ffmpeg()
    if ff and h264_encoder(ff):
        ok("ffmpeg ready: %s" % ff)
        return ff
    fail("ffmpeg could not be prepared.")
    return ""


def package_manager():
    for pm, install in (("apt-get", ["apt-get", "install", "-y"]), ("dnf", ["dnf", "install", "-y"]),
                        ("yum", ["yum", "install", "-y"]), ("pacman", ["pacman", "-S", "--noconfirm"]),
                        ("zypper", ["zypper", "--non-interactive", "install"]), ("apk", ["apk", "add"])):
        if shutil.which(pm):
            return pm, install
    return None


def pkg_install(pm, packages) -> bool:
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if pm[0] == "apt-get":
        subprocess.run(["apt-get", "update", "-qq"], env=env)
    r = subprocess.run(pm[1] + packages, env=env)
    return r.returncode == 0


# ------------------------------------------------------------------------------ status
def roles_state() -> dict:
    out = {}
    for role in ("agent", "server"):
        cfg = load_json(config_path(role))
        out[role] = {"configured": bool(cfg), "installed": service.installed(role),
                     "running": service.running(role) if cfg else False, "cfg": cfg or {}}
    return out


def panel_urls(role, cfg) -> list:
    if role == "agent":
        p = cfg.get("panel", {})
        port, bind, scheme = p.get("port", 8090), p.get("bind", "0.0.0.0"), "http"
        if bind == "127.0.0.1":
            return ["http://127.0.0.1:%s" % port]
    else:
        port, scheme = cfg.get("port", 8800), "https" if (cfg.get("tls") or {}).get("enabled") else "http"
        if cfg.get("public_address"):
            return ["%s://%s:%s" % (scheme, cfg["public_address"], port)]
    ips = local_ips() or ["127.0.0.1"]
    return ["%s://%s:%s" % (scheme, ip, port) for ip in ips[:3]]


def print_status():
    st = roles_state()
    for role, label in (("agent", "Camera agent"), ("server", "Server      ")):
        s = st[role]
        if not s["configured"]:
            state = dim("not set up")
        elif s["running"]:
            state = green("running")
        else:
            state = red("stopped")
        line = "  %s : %s" % (label, state)
        if s["configured"]:
            line += dim("   " + ", ".join(panel_urls(role, s["cfg"])))
        print(line)
    return st


def status_details():
    title("Status")
    st = print_status()
    a = st["agent"]["cfg"]
    if a:
        print()
        print(bold("  Cameras"))
        for cam in a.get("cameras", []):
            audio = ("audio: " + cam["audio"]["name"]) if cam.get("audio") else "no audio"
            print("   - %s  (%s, %sx%s @ %s fps, %s)%s" % (cam["name"], cam["device"].get("name"), cam.get("width") or
                                                         "auto", cam.get("height") or "auto", cam.get("fps"), audio,
                                                         "" if cam.get("enabled", True) else dim("  disabled")))
        print(bold("  Servers"))
        for srv in a.get("servers", []):
            print("   - %s:%s  user %s%s" % (srv["address"], srv["port"], srv["username"], "  TLS" if srv.get("tls") else ""))
        if not a.get("servers"):
            print(dim("   (none)"))
        st_cfg = a.get("storage") or {}
        print(bold("  Recording"))
        print("   %s" % (("on -> " + st_cfg.get("path", "")) if st_cfg.get("enabled") else "off"))
    s = st["server"]["cfg"]
    if s:
        print()
        print(bold("  Server"))
        print("   port %s, TLS %s, camera accounts: %s, panel users: %s" % (
            s.get("port"), "on" if (s.get("tls") or {}).get("enabled") else "off",
            ", ".join(x["username"] for x in s.get("camera_accounts", [])) or "-",
            ", ".join(x["username"] for x in s.get("users", [])) or "-"))
        known = load_json(os.path.join(dirs()["data"], "server-agents.json"), {}) or {}
        print("   camera systems seen: %d" % len(known))


# ------------------------------------------------------------------------------ camera setup
def detect(ffmpeg, include_test=False):
    print("  Detecting webcams and microphones...")
    devs = devices.list_devices(ffmpeg, include_test)
    return devs


def pick_cameras(ffmpeg, existing: list, args=None) -> list:
    """Interactive webcam selection. Returns the new list of camera configs."""
    from .agent import camera_from_device
    devs = detect(ffmpeg, include_test=bool(args and args.cameras == "test"))
    video, audio = devs["video"], devs["audio"]
    if args and args.cameras == "test":
        video = [v for v in video if v["kind"] == "test"]
    if not video:
        fail("No webcam was found on this system.")
        if IS_LINUX:
            print(dim("    Check that the webcam is plugged in: ls /dev/video*"))
        if args is None and ask_yes("Use a test pattern (virtual camera) for now?", False):
            devs = devices.list_devices(ffmpeg, True)
            video, audio = [v for v in devs["video"] if v["kind"] == "test"], devs["audio"]
        else:
            return existing
    audio_by_key = {a["key"]: a for a in audio}
    existing_by_key = {devices.device_key(cam["device"]): cam for cam in existing}
    print()
    print(bold("   #  Webcam") + " " * 28 + bold("Built-in microphone"))
    default_sel = []
    for i, v in enumerate(video, 1):
        mic = audio_by_key.get(v.get("mic"))
        extra = dim("  (%s)" % v["path"]) if v["kind"] == "v4l2" else ""
        used = green("  [in use]") if v["key"] in existing_by_key else ""
        print("  %2d) %-34s %s%s%s" % (i, v["name"][:34], mic["name"] if mic else dim("-"), extra, used))
        if v["key"] in existing_by_key:
            default_sel.append(str(i))
    print()
    if args and args.cameras:
        spec = "all" if args.cameras == "test" else args.cameras
        chosen = parse_numbers(spec, len(video))
    else:
        default = ",".join(default_sel) or "1"
        while True:
            text = ask("Which webcams? Numbers separated by commas (e.g. 1,2) or 'all'", default)
            try:
                chosen = parse_numbers(text, len(video))
                break
            except ValueError as e:
                print(red("    " + str(e)))
    result = []
    for n in chosen:
        v = video[n - 1]
        old = existing_by_key.get(v["key"])
        mic = audio_by_key.get(v.get("mic"))
        if old:
            cam = old
        else:
            print("  Reading supported resolutions of %s..." % v["name"])
            modes = devices.list_modes(ffmpeg, v)
            cam = camera_from_device({"cameras": existing + result}, v, None, v["name"], modes)
            cam["modes"] = modes
        if args is not None:
            use_mic = bool(mic) and args.audio == "yes"
        elif mic:
            print()
            use_mic = ask_yes('"%s" has a microphone (%s). Send its sound too?' % (v["name"], mic["name"]),
                              bool(old and old.get("audio")) or not old)
        else:
            use_mic = False
        if use_mic and mic:
            cam["audio"] = {k: mic[k] for k in ("kind", "path", "name", "index") if k in mic}
        elif not (old and old.get("audio") and not mic):
            cam["audio"] = None
        if args is None:
            cam["name"] = ask('Name for "%s"' % v["name"], cam["name"])[:60]
        result.append(cam)
    removed = [cam["name"] for key, cam in existing_by_key.items() if key not in {video[n - 1]["key"] for n in chosen}]
    if removed:
        warn("No longer used: %s" % ", ".join(removed))
    return result


def server_entry_wizard(agent_cfg, prefill=None) -> dict:
    from .agent import new_server_entry
    from .link import test_server
    p = prefill or {}
    while True:
        print()
        addr = ask("Server address (IP or domain)", p.get("address"))
        addr = re.sub(r"^\w+://", "", addr).split("/")[0]
        if addr.count(":") == 1 and not valid_port(addr.split(":")[1]):
            addr, port = addr.split(":")[0], int(addr.split(":")[1])
            print(dim("    Port: %d" % port))
        else:
            port = int(ask("Server port", p.get("port", 8800), valid_port))
        user = ask("Username", p.get("username"))
        pw = getpass.getpass("  Password: ")
        tls = ask_yes("Does the server use TLS (https)?", bool(p.get("tls")))
        entry = new_server_entry(addr, port, user, pw, tls)
        print("  Testing the connection...")
        r = asyncio.run(test_server(entry, agent_cfg["agent_id"], agent_cfg.get("name", "")))
        if r.get("ok"):
            ok('Connected to "%s"' % (r.get("server_name") or "server"))
            if tls and r.get("fingerprint"):
                entry["tls_pin"] = r["fingerprint"]
                print(dim("    Certificate fingerprint pinned: %s" % r["fingerprint"]))
            return entry
        fail(r.get("error", "connection failed"))
        print("    1) Try again   2) Save anyway (it keeps retrying)   3) Skip this server")
        choice = ask("Choose", "1")
        if choice == "2":
            return entry
        if choice == "3":
            return {}
        p = {"address": addr, "port": port, "username": user, "tls": tls}


def choose_drive() -> dict:
    from .storage import default_folder, list_drives
    drives = [d for d in list_drives() if d["mounted"] and not d.get("readonly")]
    if not drives:
        warn("No drives found.")
        return {}
    print()
    for i, d in enumerate(drives, 1):
        name = d["label"] or ("System disk" if d["system"] else d["mount"])
        tags = [t for t, on in (("system", d["system"]), ("removable", d.get("removable")),
                                ("network", d.get("network"))) if on]
        print("  %2d) %-22s %-24s %s free of %s %s" % (i, name[:22], d["mount"][:24], human_bytes(d["free"]),
                                                      human_bytes(d["total"]), dim(", ".join(tags))))
    if IS_LINUX:
        print(dim("      Unmounted disks can be mounted from the camera panel (Storage page)."))
    while True:
        try:
            n = parse_numbers(ask("Drive number", "1"), len(drives))[0]
            break
        except ValueError as e:
            print(red("    " + str(e)))
    d = drives[n - 1]
    folder = default_folder(d["mount"], d["system"])
    return {"enabled": True, "mount": "" if d["system"] else d["mount"], "path": folder,
            "uuid": d["id"] if IS_LINUX and not str(d["id"]).startswith("/dev/") else "",
            "device": d.get("device", ""), "label": d.get("label", ""), "fstype": d.get("fstype", "")}


def setup_camera(args=None):
    from .agent import DEFAULT_PANEL_PORT, default_config, load_config, new_server_entry
    title("Camera setup")
    existing = load_json(config_path("agent"))
    cfg = load_config() if existing else default_config()
    ff = ensure_ffmpeg(interactive=args is None)
    if not ff:
        raise Abort()
    feats = ffmpeg_features(ff)
    if "drawtext" not in feats["filters"]:
        warn("This ffmpeg cannot draw the date/time on the video (drawtext missing) - continuing without it.")
    cfg["ffmpeg"] = ""  # found automatically (system PATH or the bundled copy)

    title("Webcams")
    cfg["cameras"] = pick_cameras(ff, cfg.get("cameras", []), args)
    if not cfg["cameras"]:
        fail("No webcam selected - nothing to do.")
        raise Abort()
    for cam in cfg["cameras"]:
        ok("%s%s" % (cam["name"], dim("  + audio") if cam.get("audio") else ""))

    title("Local camera panel")
    p = cfg["panel"]
    if args is not None:
        p["port"] = int(args.panel_port or p.get("port") or DEFAULT_PANEL_PORT)
        p["username"] = args.panel_user or p.get("username") or "admin"
        if args.panel_password:
            p["secret"] = make_secret(args.panel_password)
        p["bind"] = "127.0.0.1" if args.lan == "no" else "0.0.0.0"
    else:
        cfg["name"] = ask("Name of this camera system", cfg.get("name"))[:60]
        p["port"] = int(ask("Panel port", p.get("port", DEFAULT_PANEL_PORT), valid_port))
        p["username"] = ask("Panel username", p.get("username", "admin"), valid_user)
        pw = ask_password("Panel password", keep_hint=bool(p.get("secret")))
        if pw:
            p["secret"] = make_secret(pw)
        p["bind"] = "0.0.0.0" if ask_yes("Allow other devices on your network to open this panel?",
                                         p.get("bind", "0.0.0.0") == "0.0.0.0") else "127.0.0.1"
    if not p.get("secret"):
        fail("A panel password is required.")
        raise Abort()

    title("Servers")
    if args is not None:
        if args.server:
            host, _, port = args.server.rpartition(":")
            if not host:
                host, port = args.server, "8800"
            entry = new_server_entry(host, int(port), args.server_user or "", args.server_password or "",
                                     bool(args.server_tls), "pin")
            cfg["servers"] = [s for s in cfg["servers"] if (s["address"], s["port"]) != (entry["address"], entry["port"])]
            cfg["servers"].append(entry)
    else:
        print(dim("  This system connects OUT to the server - no public IP or port forwarding is needed here."))
        for s in cfg["servers"]:
            print("   - %s:%s (user %s)" % (s["address"], s["port"], s["username"]))
        more = ask_yes("Add a server now?" if not cfg["servers"] else "Add another server?", not cfg["servers"])
        while more:
            entry = server_entry_wizard(cfg)
            if entry:
                cfg["servers"].append(entry)
            more = ask_yes("Add another server?", False)

    title("Recording")
    st = cfg.setdefault("storage", {})
    if args is not None:
        if args.record_path:
            st.update({"enabled": True, "path": args.record_path, "mount": ""})
    else:
        print(dim("  You can also choose the drive later in the panel (Storage page)."))
        if ask_yes("Record video to a disk on this system?", bool(st.get("enabled"))):
            choice = choose_drive()
            if choice:
                st.update(choice)
                ok("Recordings go to %s" % st["path"])
        else:
            st["enabled"] = False

    save_json(config_path("agent"), cfg)
    ok("Settings saved: %s" % config_path("agent"))
    title("Starting")
    try:
        how = service.install("agent")
        ok("Camera agent started (%s)" % how)
    except Exception as e:  # noqa: BLE001
        fail("Could not install the service: %s" % e)
    time.sleep(2)
    print()
    print(bold(green("  Done! Open the camera panel:")))
    for u in panel_urls("agent", cfg):
        print("    " + bold(u))
    print("    login: %s / (your password)" % cfg["panel"]["username"])


# ------------------------------------------------------------------------------ server setup
def make_self_signed(address: str) -> tuple:
    if not shutil.which("openssl"):
        raise RuntimeError("openssl is not installed")
    folder = os.path.join(dirs()["config"], "tls")
    os.makedirs(folder, exist_ok=True)
    cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    san = ("IP:" if re.match(r"^[\d.]+$|:", address) else "DNS:") + address
    base = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
            "-days", "3650", "-subj", "/CN=" + (address or "eris-cctv")]
    r = subprocess.run(base + ["-addext", "subjectAltName=" + san], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(base, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    if not IS_WINDOWS:
        os.chmod(key, 0o600)
    return cert, key


def setup_server(args=None):
    from .server import DEFAULT_PORT, cert_fingerprint_file, default_config, load_config
    title("Server setup")
    existing = load_json(config_path("server"))
    cfg = load_config() if existing else default_config()
    if args is not None:
        cfg["port"] = int(args.port or cfg.get("port") or DEFAULT_PORT)
        cam_user, cam_pw = args.camera_user or "camera", args.camera_password
        admin_user = args.admin_user or cam_user
        admin_pw = args.admin_password or cam_pw
        address = args.address or ""
        tls_mode = args.tls or "none"
        if not cam_pw:
            fail("--camera-password is required")
            raise Abort()
    else:
        cfg["port"] = int(ask("Port for the web panel and camera connections", cfg.get("port", DEFAULT_PORT),
                              valid_port))
        print()
        print(bold("  Camera connection account") + dim("  (you enter these on every webcam system)"))
        old_cam = cfg["camera_accounts"][0]["username"] if cfg["camera_accounts"] else "camera"
        cam_user = ask("Username", old_cam, valid_user)
        cam_pw = ask_password("Password", keep_hint=bool(existing and any(a["username"] == cam_user
                                                                          for a in cfg["camera_accounts"])))
        print()
        print(bold("  Web panel login") + dim("  (to watch the cameras in a browser)"))
        if ask_yes("Use the same username and password for the web panel?", not cfg["users"]):
            admin_user, admin_pw = cam_user, cam_pw
        else:
            old_admin = cfg["users"][0]["username"] if cfg["users"] else "admin"
            admin_user = ask("Panel username", old_admin, valid_user)
            admin_pw = ask_password("Panel password", keep_hint=bool(cfg["users"]))
        print()
        print("  Finding the public IP address...")
        detected = cfg.get("public_address") or public_ip()
        address = ask("Public address of this server (IP or domain)", detected)
        print()
        print("  Encryption (TLS / https):")
        print("    1) Off - plain http (simplest)")
        print("    2) On  - self-signed certificate (created now)")
        print("    3) On  - my own certificate files (e.g. Let's Encrypt)")
        current = "1"
        if (cfg.get("tls") or {}).get("enabled"):
            current = "3"
        tls_mode = {"1": "none", "2": "self-signed", "3": "files"}.get(ask("Choose", current), "none")
    # accounts
    if cam_pw:
        cfg["camera_accounts"] = [a for a in cfg["camera_accounts"] if a["username"] != cam_user]
        cfg["camera_accounts"].insert(0, {"username": cam_user, "secret": make_secret(cam_pw),
                                          "created": int(time.time()), "note": "created by setup"})
    if admin_pw:
        cfg["users"] = [u for u in cfg["users"] if u["username"] != admin_user]
        cfg["users"].insert(0, {"username": admin_user, "secret": make_secret(admin_pw), "role": "admin",
                                "cameras": "all"})
    if not cfg["camera_accounts"] or not cfg["users"]:
        fail("Both a camera account and a panel user are needed.")
        raise Abort()
    cfg["public_address"] = address
    tls = cfg.setdefault("tls", {"enabled": False, "cert": "", "key": ""})
    if tls_mode == "none":
        tls["enabled"] = False
    elif tls_mode == "self-signed":
        try:
            tls["cert"], tls["key"] = make_self_signed(address)
            tls["enabled"] = True
            ok("Self-signed certificate created")
        except Exception as e:  # noqa: BLE001
            fail("Could not create a certificate (%s) - TLS stays off." % e)
            tls["enabled"] = False
    elif tls_mode == "files":
        cert = (args.cert if args is not None else ask("Certificate file (fullchain.pem)", tls.get("cert")))
        key = (args.key if args is not None else ask("Private key file (privkey.pem)", tls.get("key")))
        if os.path.isfile(cert or "") and os.path.isfile(key or ""):
            tls.update({"enabled": True, "cert": os.path.abspath(cert), "key": os.path.abspath(key)})
        else:
            fail("Certificate or key file not found - TLS stays off.")
            tls["enabled"] = False
    save_json(config_path("server"), cfg)
    ok("Settings saved: %s" % config_path("server"))
    if IS_LINUX and is_root():
        msg = service.open_linux_firewall(int(cfg["port"]))
        if msg:
            ok(msg)
    title("Starting")
    try:
        how = service.install("server")
        ok("Server started (%s)" % how)
    except Exception as e:  # noqa: BLE001
        fail("Could not install the service: %s" % e)
    time.sleep(2)
    scheme = "https" if tls["enabled"] else "http"
    host = address or (local_ips() or ["SERVER-IP"])[0]
    print()
    print(bold(green("  Done! Web panel:")))
    print("    " + bold("%s://%s:%s" % (scheme, host, cfg["port"])) + "   login: %s" % admin_user)
    print()
    print(bold("  On every webcam system, choose \"This system is a CAMERA\" and enter:"))
    print("    Address : %s" % host)
    print("    Port    : %s" % cfg["port"])
    print("    Username: %s" % cam_user)
    print("    Password: (the camera password you chose)")
    print("    TLS     : %s" % ("yes" if tls["enabled"] else "no"))
    if tls["enabled"]:
        fp = cert_fingerprint_file(tls["cert"])
        if fp:
            print(dim("    Certificate fingerprint: %s" % fp))
    if not IS_WINDOWS:
        print(dim("  If your VPS provider has its own firewall, allow TCP port %s there too." % cfg["port"]))


# ------------------------------------------------------------------------------ manage menus
def manage_servers():
    from .link import test_server
    path = config_path("agent")
    cfg = load_json(path)
    if not cfg:
        warn("Set this system up as a camera first (option 1).")
        return
    while True:
        title("Servers this camera system sends to")
        if not cfg["servers"]:
            print(dim("   (none)"))
        for i, s in enumerate(cfg["servers"], 1):
            share = "all cameras" if s.get("cameras", "all") == "all" else "%d camera(s)" % len(s["cameras"])
            print("  %2d) %s:%s  user %s%s  - %s%s" % (i, s["address"], s["port"], s["username"],
                                                     "  TLS" if s.get("tls") else "", share,
                                                     "" if s.get("enabled", True) else dim("  (disabled)")))
        print()
        print("   a) Add a server    r) Remove a server    t) Test connections    0) Back")
        ch = ask("Choose", "0").lower()
        if ch == "a":
            entry = server_entry_wizard(cfg)
            if entry:
                cfg["servers"].append(entry)
                save_json(path, cfg)
                service.restart("agent")
                ok("Server added")
        elif ch == "r" and cfg["servers"]:
            try:
                n = parse_numbers(ask("Number of the server to remove"), len(cfg["servers"]))[0]
            except ValueError as e:
                print(red("    " + str(e)))
                continue
            s = cfg["servers"].pop(n - 1)
            save_json(path, cfg)
            service.restart("agent")
            ok("Removed %s:%s" % (s["address"], s["port"]))
        elif ch == "t":
            for s in cfg["servers"]:
                r = asyncio.run(test_server(s, cfg["agent_id"], cfg.get("name", "")))
                (ok if r.get("ok") else fail)("%s:%s - %s" % (s["address"], s["port"], "OK" if r.get("ok")
                                                               else r.get("error")))
        elif ch == "0":
            return


def manage_cameras():
    path = config_path("agent")
    cfg = load_json(path)
    if not cfg:
        warn("Set this system up as a camera first (option 1).")
        return
    ff = find_ffmpeg(cfg.get("ffmpeg", ""))
    if not ff:
        ff = ensure_ffmpeg()
        if not ff:
            return
    title("Choose webcams")
    cfg["cameras"] = pick_cameras(ff, cfg.get("cameras", []))
    save_json(path, cfg)
    service.restart("agent")
    ok("Saved - the camera agent restarted with %d camera(s)" % len(cfg["cameras"]))


def manage_server():
    path = config_path("server")
    cfg = load_json(path)
    if not cfg:
        warn("Set this system up as a server first (option 2).")
        return
    while True:
        title("Server settings")
        print("   Port %s, TLS %s, address %s" % (cfg["port"], "on" if (cfg.get("tls") or {}).get("enabled") else "off",
                                                 cfg.get("public_address") or "-"))
        print("   Camera accounts: %s" % (", ".join(a["username"] for a in cfg["camera_accounts"]) or "-"))
        print("   Panel users    : %s" % (", ".join("%s (%s)" % (u["username"], u["role"]) for u in cfg["users"]) or "-"))
        print()
        print("   1) Add a camera account          2) Change a camera account password")
        print("   3) Add a panel user (admin)      4) Reset a panel user password")
        print("   5) Change the port               6) Run the full server setup again")
        print("   0) Back")
        ch = ask("Choose", "0")
        changed = False
        if ch == "1":
            u = ask("New camera account username", validate=valid_user)
            if any(a["username"] == u for a in cfg["camera_accounts"]):
                warn("That account exists.")
                continue
            cfg["camera_accounts"].append({"username": u, "secret": make_secret(ask_password("Password")),
                                           "created": int(time.time()), "note": ""})
            changed = True
        elif ch == "2":
            u = ask("Camera account username")
            acc = next((a for a in cfg["camera_accounts"] if a["username"] == u), None)
            if not acc:
                warn("No such camera account.")
                continue
            acc["secret"] = make_secret(ask_password("New password"))
            changed = True
        elif ch == "3":
            u = ask("New panel username", validate=valid_user)
            if any(x["username"] == u for x in cfg["users"]):
                warn("That user exists.")
                continue
            cfg["users"].append({"username": u, "secret": make_secret(ask_password("Password")), "role": "admin",
                                 "cameras": "all"})
            changed = True
        elif ch == "4":
            u = ask("Panel username")
            usr = next((x for x in cfg["users"] if x["username"] == u), None)
            if not usr:
                warn("No such user.")
                continue
            usr["secret"] = make_secret(ask_password("New password"))
            changed = True
        elif ch == "5":
            cfg["port"] = int(ask("New port", cfg["port"], valid_port))
            if IS_LINUX and is_root():
                service.open_linux_firewall(cfg["port"])
            warn("Update the port on every camera system too.")
            changed = True
        elif ch == "6":
            setup_server()
            return
        elif ch == "0":
            return
        if changed:
            save_json(path, cfg)
            service.restart("server")
            ok("Saved and restarted")


def reset_password(role):
    path = config_path(role)
    cfg = load_json(path)
    if not cfg:
        warn("Not set up.")
        return
    if role == "agent":
        cfg["panel"]["username"] = ask("Panel username", cfg["panel"].get("username", "admin"), valid_user)
        cfg["panel"]["secret"] = make_secret(ask_password("New panel password"))
    else:
        manage_server()
        return
    save_json(path, cfg)
    service.restart(role)
    ok("Password changed")


def show_logs(role, lines=60):
    print()
    for line in log_tail(role, lines):
        print("  " + line)


# ------------------------------------------------------------------------------ update / uninstall
def update(restart=True):
    title("Update")
    url = "https://codeload.github.com/%s/zip/refs/heads/main" % REPO
    try:
        data = fetch(url, 120)
    except Exception as e:  # noqa: BLE001
        fail("Download failed: %s" % e)
        return False
    root = app_root()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        prefix = names[0].split("/")[0] + "/"
        version = ""
        init = prefix + "eriscctv/__init__.py"
        if init in names:
            m = re.search(r'__version__ = "([^"]+)"', z.read(init).decode())
            version = m.group(1) if m else ""
        tmp = tempfile.mkdtemp(prefix="eriscctv-update-", dir=root)
        try:
            for n in names:
                rel = n[len(prefix):]
                if not rel or not (rel.startswith("eriscctv/") or rel in ("run.py", "install.sh", "install.ps1",
                                                                          "README.md", "LICENSE")):
                    continue
                dest = os.path.join(tmp, *rel.split("/"))
                if n.endswith("/"):
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            old_pkg = os.path.join(root, "eriscctv")
            backup = os.path.join(root, "eriscctv.old")
            shutil.rmtree(backup, ignore_errors=True)
            if os.path.isdir(old_pkg):
                os.rename(old_pkg, backup)
            os.rename(os.path.join(tmp, "eriscctv"), old_pkg)
            for f in os.listdir(tmp):
                p = os.path.join(tmp, f)
                if os.path.isfile(p):
                    shutil.copy2(p, os.path.join(root, f))
            shutil.rmtree(backup, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    ok("Updated %s -> %s" % (__version__, version or "latest"))
    if restart:
        for role in ("agent", "server"):
            if load_json(config_path(role)) and service.installed(role):
                service.restart(role)
                ok("Restarted %s" % role)
    return True


def uninstall():
    title("Uninstall")
    if not ask_yes("Remove Eris CCTV from this system?", False):
        return
    for role in ("agent", "server"):
        try:
            service.uninstall(role)
        except Exception:  # noqa: BLE001
            pass
    ok("Services removed")
    cfg = load_json(config_path("agent")) or {}
    rec = (cfg.get("storage") or {}).get("path")
    if ask_yes("Delete settings (accounts, passwords, camera list)?", False):
        shutil.rmtree(dirs()["config"], ignore_errors=True)
        ok("Settings deleted")
    if rec and os.path.isdir(rec):
        if ask_yes("Delete ALL recordings in %s?" % rec, False):
            shutil.rmtree(rec, ignore_errors=True)
            ok("Recordings deleted")
        else:
            print(dim("    Recordings kept in %s" % rec))
    root = app_root()
    if IS_LINUX:
        for p in ("/usr/local/bin/eris-cctv",):
            try:
                os.remove(p)
            except OSError:
                pass
    if os.path.basename(root) in ("eris-cctv", "app") and os.path.isfile(os.path.join(root, "run.py")):
        shutil.rmtree(root, ignore_errors=True)
        if IS_WINDOWS:
            parent = os.path.dirname(root)
            for f in ("eris-cctv.cmd",):
                try:
                    os.remove(os.path.join(parent, f))
                except OSError:
                    pass
    ok("Eris CCTV removed. Goodbye!")
    sys.exit(0)


# ------------------------------------------------------------------------------ menu
def banner():
    print()
    line = "=" * 58
    print(cyan(line))
    print(bold("   %s %s") % (APP_NAME, __version__) + dim("   -   turn webcams into CCTV cameras"))
    print(cyan(line))


def need_admin():
    if IS_LINUX and not is_root():
        if shutil.which("sudo"):
            print(yellow("  Administrator rights are needed - asking sudo..."))
            os.execvp("sudo", ["sudo", sys.executable, os.path.join(app_root(), "run.py")] + sys.argv[1:])
        fail("Run this as root.")
        sys.exit(1)


def menu():
    need_admin()
    while True:
        banner()
        print_status()
        print()
        print("   1) This system is a " + bold("CAMERA") + "   - stream & record its webcams")
        print("   2) This system is a " + bold("SERVER") + "   - central panel to watch all cameras")
        print("   3) Camera: choose webcams / microphones")
        print("   4) Camera: connect to servers")
        print("   5) Server: accounts, users and port")
        print("   6) Status")
        print("   7) Restart")
        print("   8) Logs")
        print("   9) Update Eris CCTV")
        print("  10) Uninstall")
        print("   0) Exit")
        print()
        try:
            ch = ask("Choose an option", "0")
            if ch == "1":
                setup_camera()
                pause()
            elif ch == "2":
                setup_server()
                pause()
            elif ch == "3":
                manage_cameras()
                pause()
            elif ch == "4":
                manage_servers()
            elif ch == "5":
                manage_server()
            elif ch == "6":
                status_details()
                pause()
            elif ch == "7":
                for role in ("agent", "server"):
                    if load_json(config_path(role)):
                        service.restart(role)
                        ok("Restarted %s" % role)
                time.sleep(1)
            elif ch == "8":
                for role in ("agent", "server"):
                    if load_json(config_path(role)):
                        title("%s log" % role)
                        show_logs(role)
                pause()
            elif ch == "9":
                if update():
                    print(dim("  Restarting the menu with the new version..."))
                    os.execv(sys.executable, [sys.executable, os.path.join(app_root(), "run.py")])
            elif ch == "10":
                uninstall()
            elif ch == "0":
                return
        except Abort:
            print()
            warn("Cancelled.")
        except KeyboardInterrupt:
            print()
            warn("Cancelled.")


# ------------------------------------------------------------------------------ entry
def main(argv=None):
    parser = argparse.ArgumentParser(prog="eris-cctv", description="%s %s" % (APP_NAME, __version__))
    sub = parser.add_subparsers(dest="cmd")
    for role in ("agent", "server"):
        p = sub.add_parser(role, help="run the %s (used by the service)" % role)
        p.add_argument("action", choices=["run"])
        p.add_argument("-v", "--verbose", action="store_true")
    sc = sub.add_parser("setup-camera", help="set this system up as a camera (non-interactive with options)")
    sc.add_argument("--cameras", help="numbers like 1,2 or 'all' or 'test'")
    sc.add_argument("--audio", choices=["yes", "no"], default="no", help="send each webcam's own microphone")
    sc.add_argument("--panel-port", type=int)
    sc.add_argument("--panel-user")
    sc.add_argument("--panel-password")
    sc.add_argument("--lan", choices=["yes", "no"], default="yes")
    sc.add_argument("--server", help="host:port")
    sc.add_argument("--server-user")
    sc.add_argument("--server-password")
    sc.add_argument("--server-tls", action="store_true")
    sc.add_argument("--record-path")
    ss = sub.add_parser("setup-server", help="set this system up as a server (non-interactive with options)")
    ss.add_argument("--port", type=int)
    ss.add_argument("--camera-user")
    ss.add_argument("--camera-password")
    ss.add_argument("--admin-user")
    ss.add_argument("--admin-password")
    ss.add_argument("--address")
    ss.add_argument("--tls", choices=["none", "self-signed", "files"])
    ss.add_argument("--cert")
    ss.add_argument("--key")
    for name, hlp in (("status", "show status"), ("restart", "restart services"), ("start", "start services"),
                      ("stop", "stop services"), ("update", "update from GitHub"), ("uninstall", "remove"),
                      ("devices", "list webcams and microphones"), ("version", "print the version")):
        sub.add_parser(name, help=hlp)
    lg = sub.add_parser("logs", help="show logs")
    lg.add_argument("role", nargs="?", choices=["agent", "server"])
    args = parser.parse_args(argv)

    if args.cmd in ("agent", "server"):
        setup_logging(args.cmd, args.verbose)
        if args.cmd == "agent":
            from .agent import run_agent
            run_agent()
        else:
            from .server import run_server
            run_server()
        return
    if args.cmd is None:
        menu()
        return
    if args.cmd == "version":
        print("%s %s" % (APP_NAME, __version__))
        return
    if args.cmd == "devices":
        ff = find_ffmpeg()
        devs = devices.list_devices(ff)
        print(json.dumps(devs, indent=2))
        return
    need_admin()
    try:
        if args.cmd == "setup-camera":
            setup_camera(args)
        elif args.cmd == "setup-server":
            setup_server(args)
        elif args.cmd == "status":
            status_details()
        elif args.cmd in ("restart", "start", "stop"):
            for role in ("agent", "server"):
                if load_json(config_path(role)):
                    getattr(service, args.cmd)(role)
                    ok("%s: %s" % (role, args.cmd))
        elif args.cmd == "logs":
            for role in ([args.role] if args.role else ["agent", "server"]):
                if load_json(config_path(role)):
                    title("%s log" % role)
                    show_logs(role, 120)
        elif args.cmd == "update":
            update()
        elif args.cmd == "uninstall":
            uninstall()
    except Abort:
        sys.exit(1)
