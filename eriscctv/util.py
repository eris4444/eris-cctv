"""Shared helpers: paths, config files, logging and small platform utilities."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

# Hide the console window of child processes (ffmpeg) on Windows.
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def app_root() -> str:
    """Directory that contains the eriscctv package (the install directory)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_root() -> bool:
    return (not IS_WINDOWS) and os.geteuid() == 0


def dirs() -> dict:
    home = os.environ.get("ERIS_CCTV_HOME")
    if home:
        home = os.path.abspath(home)
        return {"config": os.path.join(home, "config"),
                "data": os.path.join(home, "data"),
                "logs": os.path.join(home, "logs")}
    if IS_WINDOWS:
        root = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ErisCCTV")
        return {"config": os.path.join(root, "config"),
                "data": os.path.join(root, "data"),
                "logs": os.path.join(root, "logs")}
    if is_root():
        return {"config": "/etc/eris-cctv", "data": "/var/lib/eris-cctv", "logs": "/var/log/eris-cctv"}
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return {"config": os.path.join(cfg, "eris-cctv"),
            "data": os.path.join(data, "eris-cctv"),
            "logs": os.path.join(data, "eris-cctv", "logs")}


def config_path(role: str) -> str:
    return os.path.join(dirs()["config"], role + ".json")


def data_path(*parts: str) -> str:
    return os.path.join(dirs()["data"], *parts)


def write_pid(role: str) -> None:
    try:
        os.makedirs(data_path(), exist_ok=True)
        with open(data_path(role + ".pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def remove_pid(role: str) -> None:
    try:
        os.remove(data_path(role + ".pid"))
    except OSError:
        pass


def read_pid(role: str) -> int:
    try:
        with open(data_path(role + ".pid")) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path: str, data, private: bool = True) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if private and not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def setup_logging(role: str, verbose: bool = False) -> str:
    folder = dirs()["logs"]
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, role + ".log")
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:  # pythonw has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return path


def log_tail(role: str, lines: int = 200) -> list:
    path = os.path.join(dirs()["logs"], role + ".log")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def new_id(nbytes: int = 6) -> str:
    return secrets.token_hex(nbytes)


def hostname() -> str:
    return socket.gethostname() or "camera"


def local_ips() -> list:
    """Non-loopback IPv4 addresses of this machine (best effort)."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    if IS_LINUX and shutil.which("ip"):
        try:
            out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
            found += re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.append(info[4][0])
        except OSError:
            pass
    result = []
    for ip in found:
        if not ip.startswith("127.") and ip not in result:
            result.append(ip)
    return result


def popen_kwargs() -> dict:
    return {"creationflags": NO_WINDOW} if IS_WINDOWS else {}


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def ffmpeg_candidates(configured: str = "") -> list:
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    items = [configured, os.environ.get("ERIS_CCTV_FFMPEG", ""),
             os.path.join(app_root(), "ffmpeg", "bin", exe),
             os.path.join(app_root(), "ffmpeg", exe),
             shutil.which("ffmpeg") or ""]
    return [p for p in items if p]


def find_ffmpeg(configured: str = "") -> str:
    for path in ffmpeg_candidates(configured):
        if os.path.isfile(path):
            return path
    return ""


_encoder_cache: dict = {}


def ffmpeg_features(ffmpeg: str) -> dict:
    """Encoders / filters this ffmpeg build offers (cached per binary)."""
    if ffmpeg in _encoder_cache:
        return _encoder_cache[ffmpeg]
    feats = {"encoders": set(), "filters": set(), "version": "", "input_thread_queue": False}
    if not ffmpeg:
        return feats
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                           timeout=20, **popen_kwargs())
        for line in r.stdout.splitlines():
            m = re.match(r"\s*[VAS][\.\w]{5}\s+(\S+)", line)
            if m:
                feats["encoders"].add(m.group(1))
        r = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                           timeout=20, **popen_kwargs())
        for line in r.stdout.splitlines():
            m = re.match(r"\s*[\.\w]{2,3}\s+(\w+)\s+\S+->\S+", line)
            if m:
                feats["filters"].add(m.group(1))
        r = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True,
                           timeout=20, **popen_kwargs())
        first = (r.stdout.splitlines() or [""])[0]
        m = re.match(r"ffmpeg version (\S+)", first)
        feats["version"] = m.group(1) if m else first[:40]
        # Older builds want -thread_queue_size on capture inputs; the newest reject it there.
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-thread_queue_size", "64", "-f", "lavfi",
                            "-i", "nullsrc=s=16x16:d=0.1", "-f", "null", "-"], capture_output=True, text=True,
                           timeout=20, **popen_kwargs())
        feats["input_thread_queue"] = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    _encoder_cache[ffmpeg] = feats
    return feats


def h264_encoder(ffmpeg: str) -> str:
    enc = ffmpeg_features(ffmpeg)["encoders"]
    for name in ("libx264", "libopenh264", "h264_v4l2m2m"):
        if name in enc:
            return name
    return ""


def font_file() -> str:
    """A TrueType font for the timestamp overlay, or '' when none is found."""
    if IS_WINDOWS:
        windir = os.environ.get("WINDIR", r"C:\Windows")
        names = ["consola.ttf", "arial.ttf", "segoeui.ttf", "tahoma.ttf"]
        for n in names:
            p = os.path.join(windir, "Fonts", n)
            if os.path.isfile(p):
                return p
        return ""
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
              "/usr/share/fonts/truetype/freefont/FreeMono.ttf"):
        if os.path.isfile(p):
            return p
    for base in ("/usr/share/fonts", "/usr/local/share/fonts"):
        for root, _dirs, files in os.walk(base):
            for f in sorted(files):
                if f.lower().endswith(".ttf"):
                    return os.path.join(root, f)
    return ""


def valid_name(text: str, limit: int = 60) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(text or "")).strip()
    return text[:limit]
