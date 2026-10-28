"""One ffmpeg process per camera: capture -> H.264/AAC fragmented MP4 -> subscribers."""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import time

from .mp4 import Mp4Splitter, codec_info
from .util import IS_WINDOWS, ffmpeg_features, find_ffmpeg, font_file, h264_encoder, popen_kwargs

log = logging.getLogger("camera")

QUALITY = {"low": (31, 450), "medium": (27, 1100), "high": (23, 2800)}  # (crf, kbps at 720p)
ERROR_HINTS = re.compile(r"error|could not|cannot|no such|busy|denied|invalid|failed|not found|unable",
                         re.I)


def build_command(ffmpeg: str, cam: dict, use_audio: bool = True):
    """ffmpeg arguments and working directory for a camera config."""
    dev = cam.get("device") or {}
    audio = cam.get("audio") if use_audio else None
    kind = dev.get("kind")
    w, h = int(cam.get("width") or 0), int(cam.get("height") or 0)
    fps = max(1, min(60, int(cam.get("fps") or 15)))
    fmt = cam.get("input_format") or "auto"
    feats = ffmpeg_features(ffmpeg)

    def queue(n):
        return ["-thread_queue_size", str(n)] if feats.get("input_thread_queue") else []
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning"]
    amap = None
    if kind == "v4l2":
        args += ["-f", "v4l2"] + queue(512)
        if fmt != "auto":
            args += ["-input_format", fmt]
        if w and h:
            args += ["-video_size", "%dx%d" % (w, h)]
        args += ["-framerate", str(fps), "-i", dev["path"]]
    elif kind == "dshow":
        args += ["-f", "dshow", "-rtbufsize", "100M"] + queue(512)
        if fmt == "mjpeg":
            args += ["-vcodec", "mjpeg"]
        elif fmt != "auto":
            args += ["-pixel_format", fmt]
        if w and h:
            args += ["-video_size", "%dx%d" % (w, h)]
        args += ["-framerate", str(fps)]
        if int(dev.get("index", 0)):
            args += ["-video_device_number", str(int(dev["index"]))]
        spec = "video=" + dev["name"]
        if audio and audio.get("kind") == "dshow":
            spec += ":audio=" + audio["name"]
            if int(audio.get("index", 0)):
                args += ["-audio_device_number", str(int(audio["index"]))]
            amap = "0:a:0"
        args += ["-i", spec]
    elif kind == "test":
        size = "%dx%d" % (w or 1280, h or 720)
        args += ["-re", "-f", "lavfi", "-i", "testsrc2=size=%s:rate=%d" % (size, fps)]
    else:
        raise ValueError("unknown camera type: %r" % kind)
    if audio and amap is None:
        if audio.get("kind") == "alsa":
            args += ["-f", "alsa"] + queue(1024) + ["-i", audio["path"]]
            amap = "1:a:0"
        elif audio.get("kind") == "test":
            args += ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
            amap = "1:a:0"

    vf = []
    rot = int(cam.get("rotate") or 0)
    if rot == 90:
        vf.append("transpose=1")
    elif rot == 180:
        vf.append("hflip,vflip")
    elif rot == 270:
        vf.append("transpose=2")
    if cam.get("mirror"):
        vf.append("hflip")
    vf.append("fps=%d" % fps)
    cwd = None
    if cam.get("timestamp", True) and "drawtext" in feats["filters"]:
        font = font_file()
        if font:
            cwd = os.path.dirname(font)
            size = max(14, (h or 720) // 30)
            vf.append("drawtext=fontfile=%s:text='%%{localtime\\:%%Y-%%m-%%d %%T}':x=12:y=h-th-14:"
                      "fontsize=%d:fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=6"
                      % (os.path.basename(font), size))
    vf.append("format=yuv420p")
    args += ["-map", "0:v:0"]
    if amap:
        args += ["-map", amap]
    args += ["-vf", ",".join(vf)]

    enc = h264_encoder(ffmpeg)
    if not enc:
        raise RuntimeError("this ffmpeg build has no H.264 encoder (libx264 / libopenh264)")
    crf, kbps = QUALITY.get(cam.get("quality") or "medium", QUALITY["medium"])
    area = (w or 1280) * (h or 720)
    kbps = int(kbps * max(0.35, min(2.5, area / (1280 * 720.0))))
    if cam.get("bitrate"):
        kbps = int(cam["bitrate"])
    if enc == "libx264":
        args += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "main",
                 "-crf", str(crf), "-maxrate", "%dk" % kbps, "-bufsize", "%dk" % (kbps * 2)]
    else:
        args += ["-c:v", enc, "-b:v", "%dk" % kbps]
    gop = fps
    args += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0"]
    if amap:
        args += ["-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "48000", "-af", "aresample=async=1000"]
    args += ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
             "-flush_packets", "1", "pipe:1"]
    return args, cwd


class CameraWorker:
    def __init__(self, agent, cfg: dict, slot: int):
        self.agent = agent
        self.cfg = cfg
        self.slot = slot
        self.id = cfg["id"]
        self.init = None
        self.info: dict = {}
        self.subs = set()
        self.state = "stopped"
        self.error = ""
        self.warning = ""
        self.log = collections.deque(maxlen=80)
        self.proc = None
        self._task = None
        self.started_at = 0.0
        self.last_frag = 0.0
        self.frags = 0
        self.bytes = 0
        self._rate = collections.deque(maxlen=12)
        self.audio_failures = 0
        self.audio_off_until = 0.0
        self._probe_video_only = False
        self.restarts = 0

    # -- info ------------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.cfg.get("name") or self.id

    @property
    def running(self) -> bool:
        return self.state == "running" and self.init is not None

    def bitrate(self) -> int:
        if len(self._rate) < 2:
            return 0
        span = self._rate[-1][0] - self._rate[0][0]
        if span <= 0:
            return 0
        return int(sum(n for _t, n in list(self._rate)[1:]) * 8 / span)

    def public(self) -> dict:
        return {"id": self.id, "slot": self.slot, "name": self.name, "state": self.state,
                "enabled": bool(self.cfg.get("enabled", True)),
                "error": self.error, "warning": self.warning, "online": self.running,
                "codec": self.info.get("codec", ""), "audio": bool(self.info.get("audio")),
                "width": self.info.get("width", 0), "height": self.info.get("height", 0),
                "fps": int(self.cfg.get("fps") or 15), "bitrate": self.bitrate(),
                "uptime": int(time.time() - self.started_at) if self.running else 0,
                "restarts": self.restarts}

    # -- subscribers -----------------------------------------------------------------
    def add(self, sub) -> None:
        self.subs.add(sub)

    def remove(self, sub) -> None:
        self.subs.discard(sub)

    def _emit(self, method: str, data: bytes) -> None:
        for sub in list(self.subs):
            try:
                getattr(sub, method)(self, data)
            except Exception:
                log.exception("subscriber error")

    # -- process control ---------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._kill()
        self.state = "stopped"
        self.init = None
        self._notify()

    async def restart(self) -> None:
        await self.stop()
        self.start()

    async def _kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except (asyncio.TimeoutError, Exception):
                pass

    def _notify(self) -> None:
        try:
            self.agent.camera_changed(self)
        except Exception:
            log.exception("camera_changed failed")

    def _set(self, state: str, error: str = "") -> None:
        changed = state != self.state or error != self.error
        self.state, self.error = state, error
        if changed:
            self._notify()

    def _error_from_log(self, code) -> str:
        for line in reversed(self.log):
            clean = re.sub(r"^\d\d:\d\d:\d\d\s+(\[[^\]]*\]\s*)?", "", line).strip()
            if clean and ERROR_HINTS.search(clean):
                return clean[:300]
        return "ffmpeg stopped (exit code %s)" % code

    async def _read_stderr(self, stream) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.log.append(time.strftime("%H:%M:%S ") + text)

    async def _loop(self) -> None:
        backoff = 2
        while True:
            if not self.cfg.get("enabled", True):
                self._set("disabled")
                return
            ffmpeg = find_ffmpeg(self.agent.cfg.get("ffmpeg", ""))
            if not ffmpeg:
                self._set("error", "ffmpeg was not found - run 'eris-cctv' and choose Update/Repair")
                await asyncio.sleep(30)
                continue
            want_audio = bool(self.cfg.get("audio"))
            use_audio = want_audio and time.time() >= self.audio_off_until and not self._probe_video_only
            try:
                cmd, cwd = build_command(ffmpeg, self.cfg, use_audio)
            except (ValueError, RuntimeError, KeyError) as e:
                self._set("error", str(e))
                await asyncio.sleep(30)
                continue
            self._set("starting", "")
            log.info("starting camera %s: %s", self.id, " ".join(cmd))
            self.log.append(time.strftime("%H:%M:%S ") + "starting ffmpeg%s" % ("" if use_audio else
                                                                                 " (video only)"))
            started = time.monotonic()
            got_video = False
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=cwd, **popen_kwargs())
            except OSError as e:
                self._set("error", "cannot start ffmpeg: %s" % e)
                await asyncio.sleep(15)
                continue
            proc = self.proc
            err_task = asyncio.ensure_future(self._read_stderr(proc.stderr))
            splitter = Mp4Splitter()
            reason = ""
            try:
                while True:
                    timeout = 15 if got_video else 30
                    try:
                        chunk = await asyncio.wait_for(proc.stdout.read(1 << 18), timeout)
                    except asyncio.TimeoutError:
                        reason = "no video from the camera for %d seconds" % timeout
                        break
                    if not chunk:
                        break
                    if want_audio and not use_audio and self.audio_off_until and time.time() > self.audio_off_until:
                        reason = "retrying the microphone"
                        break
                    for kind, data in splitter.feed(chunk):
                        if kind == "init":
                            self.init = data
                            self.info = codec_info(data)
                            self.started_at = time.time()
                            got_video = True
                            if self._probe_video_only:
                                # video works without the microphone, so the microphone is the problem
                                self._probe_video_only = False
                                self.audio_off_until = time.time() + 600
                                self.log.append(time.strftime("%H:%M:%S ") +
                                                "microphone failed; streaming video only, retrying audio in 10 min")
                            self.warning = "" if use_audio or not want_audio else \
                                "Microphone failed - streaming video only (audio is retried every 10 minutes)"
                            self._set("running", "")
                            self._emit("on_init", data)
                        else:
                            now = time.monotonic()
                            self.last_frag = time.time()
                            self.frags += 1
                            self.bytes += len(data)
                            self._rate.append((now, len(data)))
                            self._emit("on_frag", data)
            except ValueError as e:
                reason = "bad stream from ffmpeg: %s" % e
            finally:
                await self._kill()
                try:
                    await asyncio.wait_for(err_task, 3)
                except (asyncio.TimeoutError, Exception):
                    err_task.cancel()
            code = proc.returncode
            ran = time.monotonic() - started
            self.init = None
            self.restarts += 1
            message = reason or self._error_from_log(code)
            log.warning("camera %s stopped after %.0fs: %s", self.id, ran, message)
            self._set("error", message)
            if got_video:
                self.audio_failures = 0
            elif use_audio:
                self.audio_failures += 1
                if self.audio_failures >= 2:  # find out whether the microphone is to blame
                    self._probe_video_only = True
                    self.audio_failures = 0
                    backoff = 1
            elif self._probe_video_only:
                self._probe_video_only = False  # fails without audio too: not the microphone's fault
            if reason == "retrying the microphone":
                self.audio_off_until = 0
                self.warning = ""
                backoff = 1
            if ran > 60:
                backoff = 2
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def kill_all_orphans() -> None:
    """Windows: place this process in a job so ffmpeg children die with it."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
    except Exception:
        log.debug("job object setup failed", exc_info=True)
