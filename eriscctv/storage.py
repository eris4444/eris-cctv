"""Recording storage: drive discovery, mounting, rotating MP4 recorders and retention."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .util import IS_LINUX, IS_WINDOWS, data_path, find_ffmpeg, popen_kwargs

log = logging.getLogger("storage")

DEFAULTS = {"enabled": False, "path": "", "mount": "", "uuid": "", "device": "", "label": "",
            "segment_minutes": 10, "retention_days": 30, "min_free_gb": 2, "max_size_gb": 0}

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "f2fs",
           "jfs", "reiserfs", "hfsplus", "zfs", "nfs", "nfs4", "cifs", "smb3", "msdos"}
MOUNTABLE_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "f2fs", "hfsplus"}
SKIP_PREFIX = ("/boot", "/snap", "/var/lib/docker", "/var/snap", "/proc", "/sys", "/dev", "/run/user")


# --------------------------------------------------------------------------- drives
def _lsblk() -> list:
    if not shutil.which("lsblk"):
        return []
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o",
                              "NAME,KNAME,SIZE,FSTYPE,MOUNTPOINT,LABEL,UUID,TYPE,RM,MODEL,TRAN"],
                             capture_output=True, text=True, timeout=10).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    flat = []

    def walk(items, parent=None):
        for d in items or []:
            d["_parent"] = parent
            flat.append(d)
            walk(d.get("children"), d)
    walk(data.get("blockdevices"))
    return flat


def _unescape_mount(path: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def _linux_drives() -> list:
    blk = {}
    for d in _lsblk():
        blk["/dev/" + (d.get("kname") or d.get("name") or "")] = d
    drives, seen = [], set()
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        dev, mnt, fstype, opts = parts[0], _unescape_mount(parts[1]), parts[2], parts[3]
        if fstype not in REAL_FS or mnt in seen:
            continue
        if mnt != "/" and mnt.startswith(SKIP_PREFIX):
            continue
        seen.add(mnt)
        try:
            usage = shutil.disk_usage(mnt)
        except OSError:
            continue
        real_dev = os.path.realpath(dev) if dev.startswith("/dev/") else dev
        info = blk.get(real_dev, {})
        parent = info.get("_parent") or {}
        drives.append({"id": info.get("uuid") or real_dev, "mount": mnt, "device": real_dev,
                       "fstype": fstype, "label": info.get("label") or "",
                       "model": (parent.get("model") or info.get("model") or "").strip(),
                       "total": usage.total, "free": usage.free, "mounted": True,
                       "readonly": "ro" in opts.split(","), "system": mnt == "/",
                       "removable": str(info.get("rm") or parent.get("rm")) in ("1", "True", "true")
                       or (parent.get("tran") == "usb")})
    for d in blk.values():
        fstype = d.get("fstype") or ""
        if fstype not in MOUNTABLE_FS or d.get("mountpoint") or d.get("type") not in ("part", "disk"):
            continue
        if d.get("type") == "disk" and d.get("children"):
            continue
        parent = d.get("_parent") or {}
        drives.append({"id": d.get("uuid") or "/dev/" + d.get("kname", ""), "mount": "",
                       "device": "/dev/" + (d.get("kname") or d.get("name")), "fstype": fstype,
                       "label": d.get("label") or "", "model": (parent.get("model") or "").strip(),
                       "total": int(d.get("size") or 0), "free": 0, "mounted": False, "readonly": False,
                       "system": False, "uuid": d.get("uuid") or "",
                       "removable": parent.get("tran") == "usb" or str(parent.get("rm")) in ("1", "True")})
    return drives


def _windows_drives() -> list:
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    drives = []
    sysdrive = (os.environ.get("SystemDrive") or "C:").upper()
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(65 + i) + ":"
        root = letter + "\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype not in (2, 3, 4, 6):  # removable, fixed, network, ram
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        if not ok:
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        drives.append({"id": letter, "mount": root, "device": letter, "fstype": fs.value,
                       "label": label.value, "model": "", "total": usage.total, "free": usage.free,
                       "mounted": True, "readonly": False, "system": letter.upper() == sysdrive,
                       "removable": dtype == 2, "network": dtype == 4})
    return drives


def list_drives() -> list:
    if IS_WINDOWS:
        return _windows_drives()
    if IS_LINUX:
        return _linux_drives()
    return []


def default_folder(mount: str, system: bool) -> str:
    if system:
        return data_path("recordings")
    return os.path.join(mount, "ErisCCTV")


def mount_partition(device: str, uuid: str, fstype: str, label: str) -> str:
    """Mounts an unmounted partition under /mnt/eris-cctv/<name>; returns the mount point."""
    if not IS_LINUX:
        raise RuntimeError("Mounting drives is only supported on Linux")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", label or (uuid[:8] if uuid else os.path.basename(device)))
    target = os.path.join("/mnt/eris-cctv", name or "disk")
    os.makedirs(target, exist_ok=True)
    if os.path.ismount(target):
        return target
    source = ["UUID=" + uuid] if uuid else [device]
    cmd = ["mount"]
    if fstype == "ntfs":
        cmd += ["-t", "ntfs3" if _kernel_has("ntfs3") else "ntfs-3g"]
    cmd += source + [target]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not os.path.ismount(target):
        raise RuntimeError((r.stderr or r.stdout or "mount failed").strip()[:300])
    return target


def _kernel_has(fs: str) -> bool:
    try:
        with open("/proc/filesystems") as f:
            if fs in f.read().split():
                return True
    except OSError:
        pass
    return subprocess.run(["modprobe", "-n", fs], capture_output=True).returncode == 0 \
        if shutil.which("modprobe") else False


def _is_mounted(mount: str) -> bool:
    if IS_WINDOWS:
        return os.path.exists(mount)
    return mount == "/" or os.path.ismount(mount)


# --------------------------------------------------------------------------- recorder
def next_boundary(now: float, minutes: int) -> float:
    seg = max(1, int(minutes)) * 60
    lt = time.localtime(now)
    secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    return now + (seg - secs % seg)


def finalize(ffmpeg: str, part: str) -> None:
    """Turn a finished fragmented .mp4.part into a normal seekable .mp4."""
    final = part[:-5]
    try:
        if os.path.getsize(part) < 4096:
            os.remove(part)
            return
    except OSError:
        return
    tmp = final + ".tmp"
    ok = False
    if ffmpeg:
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", part, "-c", "copy",
                                "-map", "0", "-movflags", "+faststart", "-f", "mp4", tmp],
                               capture_output=True, timeout=600, **popen_kwargs())
            ok = r.returncode == 0 and os.path.getsize(tmp) > 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    try:
        if ok:
            os.replace(tmp, final)
            os.remove(part)
        else:
            if os.path.exists(tmp):
                os.remove(tmp)
            os.replace(part, final)
    except OSError as e:
        log.warning("could not finalize %s: %s", part, e)


class Recorder:
    """Writes one camera's fragments to <root>/<camera>/<day>/<HH-MM-SS>.mp4 (one writer thread)."""

    def __init__(self, storage, cam):
        self.storage = storage
        self.cam_id = cam.id
        self.cam_name = cam.name
        self.q = queue.Queue(maxsize=300)
        self.file = None
        self.part = ""
        self.rotate_at = 0.0
        self.init = None
        self.error = ""
        self.dropped = 0
        self.written = 0
        self.paused_until = 0.0
        self.thread = threading.Thread(target=self._run, name="rec-" + cam.id, daemon=True)
        self.thread.start()

    # called from the event loop
    def on_init(self, cam, data: bytes) -> None:
        self.cam_name = cam.name
        self._put(("init", data))

    def on_frag(self, cam, data: bytes) -> None:
        self._put(("frag", data))

    def close(self) -> None:
        self._put(("stop", None), force=True)

    def _put(self, item, force=False) -> None:
        try:
            self.q.put_nowait(item)
        except queue.Full:
            if force:
                self.q.put(item)
            else:
                self.dropped += 1

    # writer thread
    def _run(self) -> None:
        while True:
            kind, data = self.q.get()
            if kind == "stop":
                self._close()
                return
            if kind == "init":
                self._close()
                self.init = data
                continue
            if self.init is None or time.time() < self.paused_until:
                continue
            try:
                now = time.time()
                if self.file is None or now >= self.rotate_at:
                    self._close()
                    self._open(now)
                self.file.write(data)
                self.written += len(data)
                self.error = ""
            except OSError as e:
                self.error = "write failed: %s" % e
                log.warning("recording %s: %s", self.cam_id, self.error)
                self._close()
                self.paused_until = time.time() + 30

    def _open(self, now: float) -> None:
        root = self.storage.root()
        if not root:
            raise OSError("no storage folder")
        lt = time.localtime(now)
        folder = os.path.join(root, self.cam_id, time.strftime("%Y-%m-%d", lt))
        os.makedirs(folder, exist_ok=True)
        try:
            with open(os.path.join(root, self.cam_id, "camera.json"), "w", encoding="utf-8") as f:
                json.dump({"id": self.cam_id, "name": self.cam_name}, f)
        except OSError:
            pass
        self.part = os.path.join(folder, time.strftime("%H-%M-%S", lt) + ".mp4.part")
        self.file = open(self.part, "wb")
        self.file.write(self.init)
        self.rotate_at = next_boundary(now, self.storage.cfg.get("segment_minutes", 10))

    def _close(self) -> None:
        f, self.file = self.file, None
        if f is None:
            return
        try:
            f.close()
        except OSError:
            pass
        part = self.part
        self.part = ""
        self.storage.submit_finalize(part)

    @property
    def current(self) -> str:
        return self.part


# --------------------------------------------------------------------------- manager
class StorageManager:
    def __init__(self, agent):
        self.agent = agent
        self.recorders: dict = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finalize")
        self.state = "disabled"
        self.message = ""
        self.usage = {"total": 0, "free": 0, "recordings": 0, "files": 0}
        self._task = None
        self._mount_tried = 0.0

    @property
    def cfg(self) -> dict:
        st = self.agent.cfg.setdefault("storage", {})
        for k, v in DEFAULTS.items():
            st.setdefault(k, v)
        return st

    def root(self) -> str:
        return self.cfg.get("path") or ""

    def ok(self) -> bool:
        return self.state == "ok"

    def public(self) -> dict:
        c = self.cfg
        return {"enabled": bool(c["enabled"]), "state": self.state, "message": self.message,
                "path": c["path"], "mount": c["mount"], "label": c.get("label", ""),
                "device": c.get("device", ""), "segment_minutes": c["segment_minutes"],
                "retention_days": c["retention_days"], "min_free_gb": c["min_free_gb"],
                "max_size_gb": c["max_size_gb"], "usage": self.usage,
                "recording": sorted(cid for cid, r in self.recorders.items() if r.current)}

    def submit_finalize(self, part: str) -> None:
        if part:
            ffmpeg = find_ffmpeg(self.agent.cfg.get("ffmpeg", ""))
            try:
                self.pool.submit(finalize, ffmpeg, part)
            except RuntimeError:  # pool already shut down: finish it here
                finalize(ffmpeg, part)

    def shutdown(self) -> None:
        """Blocking: close every recorder, wait for its thread, then for pending finalizes."""
        recs = list(self.recorders.values())
        self.stop_all()
        for rec in recs:
            rec.thread.join(timeout=30)
        self.pool.shutdown(wait=True)

    # -- state ---------------------------------------------------------------------------
    def check(self) -> None:
        c = self.cfg
        if not c["enabled"]:
            self.state, self.message = "disabled", "Recording is turned off"
            return
        path, mount = c.get("path"), c.get("mount")
        if not path:
            self.state, self.message = "error", "No drive selected"
            return
        if mount and not _is_mounted(mount):
            if IS_LINUX and c.get("uuid") and time.time() - self._mount_tried > 120:
                self._mount_tried = time.time()
                try:
                    mount_partition(c.get("device", ""), c["uuid"], c.get("fstype", ""), c.get("label", ""))
                    log.info("re-mounted storage drive %s", c["uuid"])
                except Exception as e:  # noqa: BLE001 - reported to the panel
                    log.warning("auto-mount failed: %s", e)
            if not _is_mounted(mount):
                self.state = "error"
                self.message = "Drive not connected (%s is not available)" % mount
                return
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            self.state, self.message = "error", "Cannot create folder: %s" % e
            return
        if not os.access(path, os.W_OK):
            self.state, self.message = "error", "Folder is not writable: %s" % path
            return
        try:
            du = shutil.disk_usage(path)
            self.usage["total"], self.usage["free"] = du.total, du.free
        except OSError as e:
            self.state, self.message = "error", str(e)
            return
        errors = [r.error for r in self.recorders.values() if r.error]
        if errors:
            self.state, self.message = "error", errors[0]
            return
        self.state, self.message = "ok", "Recording"

    def sync(self) -> None:
        """Attach recorders to cameras when recording is possible, detach otherwise."""
        want = self.state == "ok"
        cams = self.agent.cameras
        for cid in list(self.recorders):
            cam = cams.get(cid)
            if not want or cam is None or not cam.cfg.get("record", True) or not cam.cfg.get("enabled", True):
                rec = self.recorders.pop(cid)
                if cam is not None:
                    cam.remove(rec)
                rec.close()
        if not want:
            return
        for cid, cam in cams.items():
            if cid in self.recorders or not cam.cfg.get("record", True) or not cam.cfg.get("enabled", True):
                continue
            rec = Recorder(self, cam)
            self.recorders[cid] = rec
            cam.add(rec)
            if cam.init:
                rec.on_init(cam, cam.init)

    def stop_all(self) -> None:
        for cid, rec in list(self.recorders.items()):
            cam = self.agent.cameras.get(cid)
            if cam:
                cam.remove(rec)
            rec.close()
        self.recorders.clear()

    def reconfigure(self) -> None:
        self.stop_all()
        self.check()
        self.sync()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.check()
        if self.root() and self.state == "ok":
            await loop.run_in_executor(None, self._finalize_leftovers)
        self.sync()
        tick = 0
        while True:
            await asyncio.sleep(15)
            tick += 1
            before = self.state
            self.check()
            self.sync()
            if before != self.state:
                log.info("storage state: %s (%s)", self.state, self.message)
            if self.state == "ok" and tick % 4 == 1:
                try:
                    await loop.run_in_executor(None, self.retention)
                except Exception:
                    log.exception("retention failed")

    # -- files -----------------------------------------------------------------------------
    def _finalize_leftovers(self) -> None:
        active = {r.current for r in self.recorders.values()}
        for dirpath, _dirs, files in os.walk(self.root()):
            for f in files:
                p = os.path.join(dirpath, f)
                if f.endswith(".mp4.part") and p not in active:
                    self.submit_finalize(p)
                elif f.endswith(".mp4.tmp"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    def _all_files(self) -> list:
        items = []
        root = self.root()
        if not root or not os.path.isdir(root):
            return items
        for cam in os.listdir(root):
            cdir = os.path.join(root, cam)
            if not os.path.isdir(cdir):
                continue
            for day in os.listdir(cdir):
                ddir = os.path.join(cdir, day)
                if not os.path.isdir(ddir):
                    continue
                for f in os.listdir(ddir):
                    if f.endswith(".mp4"):
                        p = os.path.join(ddir, f)
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        items.append((st.st_mtime, st.st_size, p))
        items.sort()
        return items

    def retention(self) -> None:
        c = self.cfg
        files = self._all_files()
        total = sum(s for _m, s, _p in files)
        now = time.time()
        days = float(c.get("retention_days") or 0)
        max_bytes = float(c.get("max_size_gb") or 0) * 1024 ** 3
        min_free = float(c.get("min_free_gb") or 0) * 1024 ** 3
        try:
            free = shutil.disk_usage(self.root()).free
        except OSError:
            free = 0
        removed = 0
        for mtime, size, path in list(files):
            too_old = days > 0 and now - mtime > days * 86400
            too_big = max_bytes > 0 and total > max_bytes
            too_full = min_free > 0 and free < min_free
            if not (too_old or too_big or too_full):
                break  # files are sorted oldest first, so nothing later qualifies either
            try:
                os.remove(path)
                total -= size
                free += size
                removed += 1
                files.remove((mtime, size, path))
                parent = os.path.dirname(path)
                if not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
        if removed:
            log.info("retention removed %d old recording(s)", removed)
        self.usage["recordings"] = total
        self.usage["files"] = len(files)

    def safe_path(self, *parts: str) -> str:
        root = os.path.realpath(self.root() or "")
        if not root or not os.path.isdir(root):
            return ""
        for p in parts:
            if not p or p in (".", "..") or "/" in p or "\\" in p or ":" in p:
                return ""
        full = os.path.realpath(os.path.join(root, *parts))
        if os.path.commonpath([root, full]) != root:
            return ""
        return full

    def camera_names(self) -> dict:
        names = {cid: cam.name for cid, cam in self.agent.cameras.items()}
        root = self.root()
        if root and os.path.isdir(root):
            for cid in os.listdir(root):
                if cid not in names:
                    try:
                        with open(os.path.join(root, cid, "camera.json"), encoding="utf-8") as f:
                            names[cid] = json.load(f).get("name") or cid
                    except (OSError, ValueError):
                        names[cid] = cid
        return names

    def list_recordings(self, cam: str = "", day: str = "") -> dict:
        root = self.root()
        out = {"cameras": [], "days": [], "files": []}
        if not root or not os.path.isdir(root):
            return out
        names = self.camera_names()
        for cid in sorted(os.listdir(root)):
            if os.path.isdir(os.path.join(root, cid)):
                out["cameras"].append({"id": cid, "name": names.get(cid, cid)})
        if cam:
            cdir = self.safe_path(cam)
            if cdir and os.path.isdir(cdir):
                for d in sorted(os.listdir(cdir), reverse=True):
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", d) and os.path.isdir(os.path.join(cdir, d)):
                        out["days"].append(d)
        if cam and day:
            ddir = self.safe_path(cam, day)
            active = {r.current for r in self.recorders.values()}
            if ddir and os.path.isdir(ddir):
                for f in sorted(os.listdir(ddir)):
                    if not (f.endswith(".mp4") or f.endswith(".mp4.part")):
                        continue
                    p = os.path.join(ddir, f)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    start = f.split(".")[0]
                    try:
                        t0 = time.mktime(time.strptime(day + " " + start, "%Y-%m-%d %H-%M-%S"))
                    except ValueError:
                        t0 = st.st_mtime
                    out["files"].append({"name": f, "start": start.replace("-", ":"),
                                         "size": st.st_size, "duration": max(0, int(st.st_mtime - t0)),
                                         "recording": p in active or f.endswith(".part"),
                                         "ts": int(t0)})
        return out
