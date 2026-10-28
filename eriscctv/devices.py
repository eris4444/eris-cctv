"""Webcam and microphone discovery for Linux (V4L2 / ALSA) and Windows (DirectShow via ffmpeg)."""
from __future__ import annotations

import glob
import os
import re
import struct
import subprocess

from .util import IS_LINUX, IS_WINDOWS, popen_kwargs

TEST_VIDEO = {"kind": "test", "path": "testsrc", "name": "Test pattern (virtual camera)", "index": 0}
TEST_AUDIO = {"kind": "test", "path": "sine", "name": "Test tone (440 Hz)", "index": 0}

# Linux drivers that expose capture nodes which are not webcams (codecs, ISPs, raw CSI receivers).
NON_CAMERA_DRIVERS = {"bcm2835-codec", "bcm2835-isp", "rpivid", "pispbe", "rp1-cfe", "unicam",
                      "cedrus", "hantro-vpu", "rkvdec", "bcm2835_v4l2_codec"}


def device_key(dev: dict) -> str:
    if not dev:
        return ""
    if dev.get("kind") == "dshow":
        return "dshow:%s#%d" % (dev.get("name", ""), int(dev.get("index", 0)))
    return "%s:%s" % (dev.get("kind", ""), dev.get("path", ""))


def _run(cmd, timeout=20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           **popen_kwargs())
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------- Linux
def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _usb_parent(sys_device_link: str) -> str:
    try:
        real = os.path.realpath(sys_device_link)
    except OSError:
        return ""
    if "/usb" not in real:
        return ""
    return os.path.dirname(real)


def _querycap(dev: str):
    import fcntl  # Linux only
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, 0x80685600, buf, True)  # VIDIOC_QUERYCAP
    finally:
        os.close(fd)

    def text(a, b):
        return bytes(buf[a:b]).split(b"\0")[0].decode("utf-8", "replace")
    _version, caps, dcaps = struct.unpack_from("=III", buf, 80)
    if caps & 0x80000000:  # V4L2_CAP_DEVICE_CAPS
        caps = dcaps
    return text(0, 16), text(16, 48), text(48, 80), caps


def _linux_video() -> list:
    by_id = {}
    for link in glob.glob("/dev/v4l/by-id/*"):
        target = os.path.realpath(link)
        prev = by_id.get(target)
        if prev is None or ("index0" in link and "index0" not in prev):
            by_id[target] = link
    found = []
    for sysdir in sorted(glob.glob("/sys/class/video4linux/video*"),
                         key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
        node = "/dev/" + os.path.basename(sysdir)
        if not os.path.exists(node):
            continue
        name = _read(os.path.join(sysdir, "name")) or os.path.basename(node)
        bus = ""
        try:
            driver, card, bus, caps = _querycap(node)
            if driver in NON_CAMERA_DRIVERS:
                continue
            if not caps & 0x1 or caps & (0x8000 | 0x4000):  # needs VIDEO_CAPTURE, no M2M
                continue
            name = card or name
        except OSError:
            if _read(os.path.join(sysdir, "index")) not in ("", "0"):
                continue
        found.append({"kind": "v4l2", "path": by_id.get(node, node), "node": node, "name": name,
                      "bus": bus, "usb": _usb_parent(os.path.join(sysdir, "device")), "index": 0})
    return found


def _alsa_card_names() -> dict:
    names = {}
    for line in _read("/proc/asound/cards").splitlines():
        m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*)$", line)
        if m:
            desc = m.group(3)
            names[m.group(1)] = desc.split(" - ", 1)[1].strip() if " - " in desc else desc.strip()
    return names


def _linux_audio() -> list:
    names = _alsa_card_names()
    found = []
    for carddir in sorted(glob.glob("/proc/asound/card[0-9]*")):
        num = re.sub(r"\D", "", os.path.basename(carddir))
        card_id = _read(os.path.join(carddir, "id")) or num
        usb = _usb_parent("/sys/class/sound/card%s/device" % num)
        for pcm in sorted(glob.glob(os.path.join(carddir, "pcm*c"))):
            dev = re.sub(r"\D", "", os.path.basename(pcm))
            label = names.get(num, card_id)
            pcm_name = ""
            for line in _read(os.path.join(pcm, "info")).splitlines():
                if line.startswith("name:"):
                    pcm_name = line.split(":", 1)[1].strip()
            if dev != "0" and pcm_name:
                label += " - " + pcm_name
            found.append({"kind": "alsa", "path": "plughw:CARD=%s,DEV=%s" % (card_id, dev),
                          "name": label, "usb": usb, "index": 0})
    return found


# --------------------------------------------------------------------------- Windows
def _dshow_devices(ffmpeg: str):
    out = _run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
    video, audio = [], []
    section = None
    pending = None
    for line in out.splitlines():
        body = re.sub(r"^\[[^\]]*\]\s*", "", line)
        if "DirectShow video devices" in body:
            section = "video"
            continue
        if "DirectShow audio devices" in body:
            section = "audio"
            continue
        m = re.match(r'"(.+)"\s*\(([^)]*)\)\s*$', body)
        if m:
            kinds = m.group(2)
            target = video if "video" in kinds else audio if "audio" in kinds else None
            pending = {"kind": "dshow", "name": m.group(1), "alt": ""}
            if target is not None:
                target.append(pending)
            continue
        m = re.match(r'"(.+)"\s*$', body)
        if m and section:
            pending = {"kind": "dshow", "name": m.group(1), "alt": ""}
            (video if section == "video" else audio).append(pending)
            continue
        m = re.match(r'Alternative name\s+"(.+)"', body)
        if m and pending is not None:
            pending["alt"] = m.group(1)
    for group in (video, audio):
        seen = {}
        for d in group:
            d["index"] = seen.get(d["name"], 0)
            seen[d["name"]] = d["index"] + 1
            d["path"] = d["name"]
    return video, audio


# --------------------------------------------------------------------------- public API
def list_devices(ffmpeg: str, include_test: bool = False) -> dict:
    video, audio = [], []
    if IS_LINUX:
        video, audio = _linux_video(), _linux_audio()
    elif IS_WINDOWS and ffmpeg:
        video, audio = _dshow_devices(ffmpeg)
    for v in video:
        mic = suggest_mic(v, audio)
        v["mic"] = device_key(mic) if mic else ""
        v["key"] = device_key(v)
    for a in audio:
        a["key"] = device_key(a)
    if include_test:
        video.append(dict(TEST_VIDEO, key=device_key(TEST_VIDEO), mic=device_key(TEST_AUDIO)))
        audio.append(dict(TEST_AUDIO, key=device_key(TEST_AUDIO)))
    return {"video": video, "audio": audio}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def suggest_mic(video: dict, audios: list):
    """The microphone that belongs to the same physical webcam, if any."""
    if video.get("usb"):
        for a in audios:
            if a.get("usb") and a["usb"] == video["usb"]:
                return a
    vname = _norm(video.get("name", ""))
    vname = re.sub(r"^(usb ?2 ?0|usb) ", "", vname)
    if len(vname) < 3:
        return None
    for a in audios:
        if vname and vname in _norm(a.get("name", "")):
            return a
    return None


def find_device(devices: list, key: str):
    for d in devices:
        if d.get("key") == key:
            return d
    return None


def list_modes(ffmpeg: str, dev: dict) -> list:
    """Supported capture modes: [{'format','width','height','fps'}] (fps 0 = unknown)."""
    if not ffmpeg or not dev:
        return []
    modes = {}
    if dev.get("kind") == "test":
        for w, h in ((640, 480), (1280, 720), (1920, 1080)):
            modes[("raw", w, h)] = {"format": "raw", "width": w, "height": h, "fps": 30}
    elif dev.get("kind") == "v4l2":
        out = _run([ffmpeg, "-hide_banner", "-f", "v4l2", "-list_formats", "all", "-i", dev["path"]])
        for line in out.splitlines():
            m = re.search(r"(Raw|Compressed)\s*:\s*(\S+)\s*:[^:]*:\s*(.*)$", line)
            if not m:
                continue
            fmt = m.group(2)
            if m.group(1) == "Compressed" and fmt != "mjpeg":
                continue
            for w, h in re.findall(r"(?<![\d{,])(\d{2,5})x(\d{2,5})(?![\d}])", m.group(3)):
                modes[(fmt, int(w), int(h))] = {"format": fmt, "width": int(w), "height": int(h), "fps": 0}
    elif dev.get("kind") == "dshow":
        cmd = [ffmpeg, "-hide_banner", "-f", "dshow", "-list_options", "true"]
        if int(dev.get("index", 0)):
            cmd += ["-video_device_number", str(int(dev["index"]))]
        out = _run(cmd + ["-i", "video=" + dev["name"]])
        for line in out.splitlines():
            m = re.search(r"(vcodec|pixel_format)=(\S+)\s+min s=\d+x\d+ fps=[\d.]+\s+"
                          r"max s=(\d+)x(\d+) fps=([\d.]+)", line)
            if not m:
                continue
            fmt = m.group(2)
            if m.group(1) == "vcodec" and fmt != "mjpeg":
                continue
            w, h, fps = int(m.group(3)), int(m.group(4)), int(float(m.group(5)))
            k = (fmt, w, h)
            if k not in modes or modes[k]["fps"] < fps:
                modes[k] = {"format": fmt, "width": w, "height": h, "fps": fps}
    return sorted(modes.values(), key=lambda m: (-m["width"] * m["height"], m["format"]))


def sizes(modes: list) -> list:
    out = []
    for m in modes:
        s = "%dx%d" % (m["width"], m["height"])
        if s not in out:
            out.append(s)
    return out


def default_size(modes: list):
    if not modes:
        return 0, 0
    area = [(m["width"] * m["height"], m["width"], m["height"]) for m in modes]
    for a, w, h in area:
        if (w, h) == (1280, 720):
            return w, h
    fit = sorted([x for x in area if x[0] <= 1280 * 720], reverse=True)
    if fit:
        return fit[0][1], fit[0][2]
    smallest = min(area)
    return smallest[1], smallest[2]


def pick_input(modes: list, width: int, height: int, fps: int):
    """Choose input pixel format and frame rate for a size: ('auto'|'mjpeg'|raw fmt, fps)."""
    same = [m for m in modes if m["width"] == width and m["height"] == height]
    if not same:
        return "auto", fps
    raw = [m for m in same if m["format"] != "mjpeg"]
    mjpeg = [m for m in same if m["format"] == "mjpeg"]
    for m in raw:
        if m["fps"] >= fps or (m["fps"] == 0 and width * height <= 640 * 480):
            return m["format"], fps
    if mjpeg:
        m = mjpeg[0]
        return "mjpeg", min(fps, m["fps"]) if m["fps"] else fps
    m = raw[0]
    return m["format"], min(fps, m["fps"]) if m["fps"] else fps
