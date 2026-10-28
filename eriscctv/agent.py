"""Camera agent: runs the webcams, records, connects to servers and serves the local panel."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import time

from . import __version__, devices
from .auth import check_secret, make_secret
from .camera import CameraWorker, kill_all_orphans
from .link import TLS_MODES, ServerLink, test_server
from .panel import PanelAuth, as_float, as_int, need_login
from .storage import StorageManager, default_folder, list_drives, mount_partition
from .util import (IS_WINDOWS, config_path, ffmpeg_features, find_ffmpeg, hostname, load_json, local_ips,
                   log_tail, new_id, remove_pid, save_json, valid_name, write_pid)
from .web import FileResponse, HttpError, HttpServer
from .ws import WebSocketClosed

log = logging.getLogger("agent")

DEFAULT_PANEL_PORT = 8090
QUALITIES = ("low", "medium", "high")


def default_config() -> dict:
    return {"agent_id": new_id(8), "name": hostname(), "ffmpeg": "",
            "panel": {"port": DEFAULT_PANEL_PORT, "bind": "0.0.0.0", "username": "admin", "secret": None},
            "cameras": [], "servers": [], "storage": {}}


def load_config(path: str = "") -> dict:
    path = path or config_path("agent")
    cfg = load_json(path) or default_config()
    base = default_config()
    for k, v in base.items():
        cfg.setdefault(k, v)
    for k, v in base["panel"].items():
        cfg["panel"].setdefault(k, v)
    for s in cfg["servers"]:
        if s.get("tls_mode") not in TLS_MODES:
            # 1.0 had tls: true/false; "false" usually meant "the server had no TLS yet"
            s["tls_mode"] = "on" if s.get("tls") is True else "auto"
        s.pop("tls", None)
    return cfg


def camera_from_device(cfg: dict, dev: dict, audio, name: str = "", modes=None) -> dict:
    """New camera entry with sensible defaults for a discovered device."""
    modes = modes or []
    w, h = devices.default_size(modes)
    fps = 15
    fmt, fps = devices.pick_input(modes, w, h, fps)
    used = {c["id"] for c in cfg.get("cameras", [])}
    cid = "cam" + new_id(3)
    while cid in used:
        cid = "cam" + new_id(3)
    return {"id": cid, "name": valid_name(name or dev.get("name") or "Camera") or "Camera",
            "enabled": True, "record": True,
            "device": {k: dev[k] for k in ("kind", "path", "name", "index") if k in dev},
            "audio": ({k: audio[k] for k in ("kind", "path", "name", "index") if k in audio} if audio else None),
            "width": w, "height": h, "fps": fps or 15, "input_format": fmt, "quality": "medium",
            "rotate": 0, "mirror": False, "timestamp": True}


def new_server_entry(address: str, port: int, username: str, password: str, tls_mode: str = "auto",
                     verify: str = "pin", cameras="all") -> dict:
    return {"id": "srv" + new_id(3), "address": address.strip(), "port": int(port), "username": username,
            "password": password, "tls_mode": tls_mode if tls_mode in TLS_MODES else "auto",
            "tls_verify": verify, "tls_pin": "", "enabled": True, "cameras": cameras}


class LiveViewer:
    """A browser watching one camera on the local panel."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=48)
        self.primed = False

    def _put(self, item, force=False):
        try:
            self.q.put_nowait(item)
        except asyncio.QueueFull:
            if force:
                while not self.q.empty():
                    self.q.get_nowait()
                self.q.put_nowait(item)

    def on_init(self, cam, data):
        self._put((0, json.dumps(meta(cam))), force=True)
        self._put((1, data), force=True)
        self.primed = True

    def on_frag(self, cam, data):
        if self.primed:
            self._put((2, data))

    def on_state(self, cam):
        if not cam.running:
            self.primed = False
            self._put((0, json.dumps(meta(cam))), force=True)


def meta(cam) -> dict:
    p = cam.public()
    return {"type": "meta", "name": p["name"], "online": p["online"], "codec": p["codec"],
            "audio": p["audio"], "state": p["state"], "error": p["error"]}


class Agent:
    def __init__(self, path: str = ""):
        self.path = path or config_path("agent")
        self.cfg = load_config(self.path)
        self.cameras: dict = {}
        self.links: dict = {}
        self.storage = StorageManager(self)
        self.web = HttpServer("eris-cctv-camera")
        self.started = time.time()
        self._slot = 0
        self._devices_cache = {"video": [], "audio": []}
        self._stop = None
        self.auth = PanelAuth(self.web, "ecctv_cam", self._verify, "agent")
        self._routes()

    # -- basics ------------------------------------------------------------------------
    @property
    def agent_id(self) -> str:
        return self.cfg["agent_id"]

    @property
    def name(self) -> str:
        return self.cfg.get("name") or hostname()

    def save(self) -> None:
        save_json(self.path, self.cfg)

    def _verify(self, username: str, password: str):
        p = self.cfg["panel"]
        if p.get("secret") and username == p.get("username") and check_secret(p["secret"], password):
            return "admin"
        return None

    def camera_changed(self, cam) -> None:
        for link in self.links.values():
            link.cameras_changed()
        for sub in list(cam.subs):
            fn = getattr(sub, "on_state", None)
            if fn:
                fn(cam)

    def _add_worker(self, cfg: dict) -> CameraWorker:
        self._slot += 1
        cam = CameraWorker(self, cfg, self._slot)
        self.cameras[cfg["id"]] = cam
        for link in self.links.values():
            if link.state == "connected":
                cam.add(link)
        cam.start()
        return cam

    def _add_link(self, cfg: dict) -> ServerLink:
        link = ServerLink(self, cfg)
        self.links[cfg["id"]] = link
        link.start()
        return link

    # -- run ---------------------------------------------------------------------------
    async def run(self) -> None:
        kill_all_orphans()
        self._stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if not IS_WINDOWS:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, self._stop.set)
                except (NotImplementedError, RuntimeError):
                    pass
        p = self.cfg["panel"]
        await self.web.start(p.get("bind", "0.0.0.0"), int(p.get("port", DEFAULT_PANEL_PORT)))
        log.info("Eris CCTV camera agent %s - panel on http://%s:%s", __version__,
                 p.get("bind", "0.0.0.0"), p.get("port"))
        for c in self.cfg["cameras"]:
            self._add_worker(c)
        for s in self.cfg["servers"]:
            self._add_link(s)
        storage_task = asyncio.ensure_future(self.storage.run())
        write_pid("agent")
        try:
            await self._stop.wait()
        finally:
            log.info("stopping")
            storage_task.cancel()
            await self.web.stop()
            for link in list(self.links.values()):
                await link.stop()
            for cam in list(self.cameras.values()):
                await cam.stop()
            await loop.run_in_executor(None, self.storage.shutdown)
            remove_pid("agent")

    def stop(self) -> None:
        if self._stop:
            self._stop.set()

    # -- status ------------------------------------------------------------------------
    def status(self) -> dict:
        ffmpeg = find_ffmpeg(self.cfg.get("ffmpeg", ""))
        cams = []
        for c in self.cfg["cameras"]:
            w = self.cameras.get(c["id"])
            item = dict(c)
            live = w.public() if w else {}
            item["live"] = live
            for k in ("state", "error", "warning", "online", "bitrate", "codec", "uptime", "restarts", "slot"):
                item[k] = live.get(k, "" if k in ("state", "error", "warning", "codec") else 0)
            item["has_audio"] = bool(live.get("audio"))
            item["recording"] = c["id"] in self.storage.recorders and bool(self.storage.recorders[c["id"]].current)
            cams.append(item)
        return {"agent": {"id": self.agent_id, "name": self.name, "version": __version__,
                          "hostname": hostname(), "platform": platform.platform(terse=True),
                          "uptime": int(time.time() - self.started), "ffmpeg": ffmpeg,
                          "ffmpeg_version": ffmpeg_features(ffmpeg)["version"] if ffmpeg else ""},
                "cameras": cams, "servers": [lk.public() for lk in self.links.values()],
                "storage": self.storage.public()}

    # -- routes ------------------------------------------------------------------------
    def _routes(self) -> None:
        web = self.web
        r = web.route

        @r("GET", "/api/session")
        def session(req):
            s = req.session
            return {"logged_in": bool(s), "username": s["username"] if s else "", "app": "camera",
                    "name": self.name if s else "", "version": __version__}

        @r("GET", "/api/status")
        def status(req):
            need_login(req)
            return self.status()

        @r("GET", "/api/devices")
        async def list_devs(req):
            need_login(req)
            ffmpeg = find_ffmpeg(self.cfg.get("ffmpeg", ""))
            loop = asyncio.get_running_loop()
            devs = await loop.run_in_executor(None, devices.list_devices, ffmpeg, True)
            self._devices_cache = devs
            used = {devices.device_key(c["device"]): c["name"] for c in self.cfg["cameras"]}
            for v in devs["video"]:
                v["used_by"] = used.get(v["key"], "")
            return devs

        @r("GET", "/api/devices/modes")
        async def modes(req):
            need_login(req)
            dev = self._device(req.query.get("key", ""))
            ffmpeg = find_ffmpeg(self.cfg.get("ffmpeg", ""))
            if dev is None:
                return {"modes": [], "sizes": []}
            busy = any(devices.device_key(c["device"]) == dev["key"] for c in self.cfg["cameras"])
            if busy and dev["kind"] == "dshow":
                cam = next(c for c in self.cfg["cameras"] if devices.device_key(c["device"]) == dev["key"])
                return {"modes": cam.get("modes", []), "sizes": devices.sizes(cam.get("modes", []))}
            loop = asyncio.get_running_loop()
            ms = await loop.run_in_executor(None, devices.list_modes, ffmpeg, dev)
            return {"modes": ms, "sizes": devices.sizes(ms)}

        @r("POST", "/api/cameras")
        async def add_camera(req):
            need_login(req)
            data = req.json()
            dev = self._device(data.get("device", ""))
            if dev is None:
                raise HttpError(400, "Choose a camera device (refresh the device list)")
            if any(devices.device_key(c["device"]) == dev["key"] for c in self.cfg["cameras"]):
                raise HttpError(409, "This device is already added")
            audio = self._audio(data.get("audio", ""))
            ffmpeg = find_ffmpeg(self.cfg.get("ffmpeg", ""))
            loop = asyncio.get_running_loop()
            ms = await loop.run_in_executor(None, devices.list_modes, ffmpeg, dev)
            cam = camera_from_device(self.cfg, dev, audio, data.get("name", ""), ms)
            cam["modes"] = ms
            self._apply_camera(cam, data, ms)
            self.cfg["cameras"].append(cam)
            self.save()
            self._add_worker(cam)
            self.storage.sync()
            return {"ok": True, "id": cam["id"]}

        @r("PUT", "/api/cameras/{cid}")
        async def edit_camera(req):
            need_login(req)
            cam = self._camera_cfg(req.params["cid"])
            data = req.json()
            if "audio" in data:
                audio = self._audio(data.get("audio") or "") if data.get("audio") else None
                if data.get("audio") and audio is None:
                    known = cam.get("audio")
                    if not (known and devices.device_key(known) == data["audio"]):
                        raise HttpError(400, "Microphone not found - refresh the device list")
                    audio = known
                cam["audio"] = ({k: audio[k] for k in ("kind", "path", "name", "index") if k in audio}
                                if audio else None)
            self._apply_camera(cam, data, cam.get("modes", []))
            self.save()
            w = self.cameras.get(cam["id"])
            if w:
                await w.restart()
            self.storage.sync()
            return {"ok": True}

        @r("DELETE", "/api/cameras/{cid}")
        async def del_camera(req):
            need_login(req)
            cam = self._camera_cfg(req.params["cid"])
            self.cfg["cameras"] = [c for c in self.cfg["cameras"] if c["id"] != cam["id"]]
            for s in self.cfg["servers"]:
                if isinstance(s.get("cameras"), list) and cam["id"] in s["cameras"]:
                    s["cameras"].remove(cam["id"])
            self.save()
            w = self.cameras.pop(cam["id"], None)
            self.storage.sync()
            if w:
                await w.stop()
            for link in self.links.values():
                link.cameras_changed()
            return {"ok": True}

        @r("POST", "/api/cameras/{cid}/restart")
        async def restart_camera(req):
            need_login(req)
            cam = self._camera_cfg(req.params["cid"])
            w = self.cameras.get(cam["id"])
            if w:
                await w.restart()
            return {"ok": True}

        @r("GET", "/api/cameras/{cid}/log")
        def camera_log(req):
            need_login(req)
            w = self.cameras.get(req.params["cid"])
            if not w:
                raise HttpError(404, "Camera not found")
            return {"lines": list(w.log)}

        # servers
        @r("POST", "/api/servers")
        def add_server(req):
            need_login(req)
            data = req.json()
            entry = new_server_entry("", 0, "", "")
            self._apply_server(entry, data, new=True)
            self.cfg["servers"].append(entry)
            self.save()
            self._add_link(entry)
            return {"ok": True, "id": entry["id"]}

        @r("PUT", "/api/servers/{sid}")
        async def edit_server(req):
            need_login(req)
            entry = self._server_cfg(req.params["sid"])
            old = (entry["address"], entry["port"])
            self._apply_server(entry, req.json(), new=False)
            if (entry["address"], entry["port"]) != old:
                entry["tls_pin"] = ""
            self.save()
            link = self.links.pop(entry["id"], None)
            if link:
                await link.stop()
            self._add_link(entry)
            return {"ok": True}

        @r("DELETE", "/api/servers/{sid}")
        async def del_server(req):
            need_login(req)
            entry = self._server_cfg(req.params["sid"])
            self.cfg["servers"] = [s for s in self.cfg["servers"] if s["id"] != entry["id"]]
            self.save()
            link = self.links.pop(entry["id"], None)
            if link:
                await link.stop()
            return {"ok": True}

        @r("POST", "/api/servers/test")
        async def test(req):
            need_login(req)
            data = req.json()
            base = dict(self._server_cfg(data["id"])) if data.get("id") else new_server_entry("", 0, "", "")
            self._apply_server(base, data, new=not data.get("id"))
            return await test_server(base, self.agent_id, self.name)

        # storage
        @r("GET", "/api/storage/drives")
        async def drives(req):
            need_login(req)
            loop = asyncio.get_running_loop()
            items = await loop.run_in_executor(None, list_drives)
            for d in items:
                d["folder"] = default_folder(d["mount"], d["system"]) if d["mounted"] else ""
            return {"drives": items, "current": self.storage.public()}

        @r("POST", "/api/storage/mount")
        async def mount(req):
            need_login(req)
            d = req.json()
            loop = asyncio.get_running_loop()
            try:
                target = await loop.run_in_executor(None, mount_partition, str(d.get("device", "")),
                                                    str(d.get("uuid", "")), str(d.get("fstype", "")),
                                                    str(d.get("label", "")))
            except Exception as e:  # noqa: BLE001 - shown to the user
                raise HttpError(400, "Mount failed: %s" % e)
            return {"ok": True, "mount": target, "folder": default_folder(target, False)}

        @r("PUT", "/api/storage")
        def set_storage(req):
            need_login(req)
            d = req.json()
            st = self.storage.cfg
            if "enabled" in d:
                st["enabled"] = bool(d["enabled"])
            if "drive" in d and d["drive"]:
                drv = d["drive"]
                mount_point = str(drv.get("mount", ""))
                folder = str(d.get("path") or "").strip() or default_folder(mount_point, bool(drv.get("system")))
                st.update({"mount": "" if drv.get("system") else mount_point, "path": folder,
                           "uuid": str(drv.get("uuid") or (drv.get("id") if not IS_WINDOWS else "") or ""),
                           "device": str(drv.get("device", "")), "label": str(drv.get("label", "")),
                           "fstype": str(drv.get("fstype", ""))})
                if IS_WINDOWS:
                    st["uuid"] = ""
                if st["uuid"].startswith("/dev/"):
                    st["uuid"] = ""
            elif d.get("path"):
                st["path"] = str(d["path"]).strip()
            if st["enabled"] and not st.get("path"):
                raise HttpError(400, "Choose a drive for the recordings first")
            st["segment_minutes"] = as_int(d.get("segment_minutes", st["segment_minutes"]), 10, 1, 120)
            st["retention_days"] = as_int(d.get("retention_days", st["retention_days"]), 30, 0, 3650)
            st["min_free_gb"] = as_float(d.get("min_free_gb", st["min_free_gb"]), 2, 0, 100000)
            st["max_size_gb"] = as_float(d.get("max_size_gb", st["max_size_gb"]), 0, 0, 1000000)
            self.save()
            self.storage.reconfigure()
            return {"ok": True, "storage": self.storage.public()}

        @r("GET", "/api/recordings")
        def recordings(req):
            need_login(req)
            return self.storage.list_recordings(req.query.get("camera", ""), req.query.get("day", ""))

        @r("GET", "/rec/{cam}/{day}/{name}")
        def rec_file(req):
            need_login(req)
            p = self.storage.safe_path(req.params["cam"], req.params["day"], req.params["name"])
            if not p or not os.path.isfile(p):
                raise HttpError(404, "Recording not found")
            dl = req.query.get("download") == "1"
            name = "%s_%s_%s" % (req.params["cam"], req.params["day"], req.params["name"].replace(".part", ""))
            return FileResponse(p, "video/mp4", download_name=name if dl else "")

        @r("DELETE", "/api/recordings")
        def del_recordings(req):
            need_login(req)
            d = req.json()
            cam, day, name = str(d.get("camera", "")), str(d.get("day", "")), str(d.get("file", ""))
            active = {rec.current for rec in self.storage.recorders.values()}
            removed = 0
            if name:
                p = self.storage.safe_path(cam, day, name)
                targets = [p] if p and os.path.isfile(p) else []
            else:
                folder = self.storage.safe_path(cam, day)
                targets = [os.path.join(folder, f) for f in os.listdir(folder)] if folder and \
                    os.path.isdir(folder) else []
            for p in targets:
                if p in active or not (p.endswith(".mp4") or p.endswith(".part")):
                    continue
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
            return {"ok": True, "removed": removed}

        # settings
        @r("GET", "/api/settings")
        def settings(req):
            need_login(req)
            p = self.cfg["panel"]
            return {"name": self.name, "port": p["port"], "bind": p["bind"], "username": p["username"],
                    "ffmpeg": self.cfg.get("ffmpeg", ""), "agent_id": self.agent_id,
                    "ffmpeg_found": find_ffmpeg(self.cfg.get("ffmpeg", ""))}

        @r("PUT", "/api/settings")
        def set_settings(req):
            need_login(req)
            d = req.json()
            restart = False
            if "name" in d:
                name = valid_name(d["name"])
                if not name:
                    raise HttpError(400, "Name cannot be empty")
                self.cfg["name"] = name
                for link in self.links.values():
                    link.cameras_changed()
            p = self.cfg["panel"]
            if "port" in d:
                port = as_int(d["port"], p["port"], 1, 65535)
                restart = restart or port != p["port"]
                p["port"] = port
            if "bind" in d and d["bind"] in ("0.0.0.0", "127.0.0.1"):
                restart = restart or d["bind"] != p["bind"]
                p["bind"] = d["bind"]
            if "ffmpeg" in d:
                path = str(d["ffmpeg"]).strip()
                if path and not os.path.isfile(path):
                    raise HttpError(400, "ffmpeg not found at that path")
                self.cfg["ffmpeg"] = path
            self.save()
            return {"ok": True, "restart_needed": restart}

        @r("POST", "/api/password")
        async def password(req):
            s = need_login(req)
            d = req.json()
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, self._verify, s["username"], str(d.get("current", "")))
            if not ok:
                raise HttpError(400, "Current password is wrong")
            new = str(d.get("password", ""))
            if len(new) < 6:
                raise HttpError(400, "Use at least 6 characters")
            user = valid_name(d.get("username") or self.cfg["panel"]["username"], 40)
            self.cfg["panel"]["username"] = user
            self.cfg["panel"]["secret"] = await loop.run_in_executor(None, make_secret, new)
            self.save()
            self.auth.sessions.drop_user(s["username"], keep=req.session_token)
            req.session["username"] = user
            return {"ok": True}

        @r("GET", "/api/logs")
        def logs(req):
            need_login(req)
            return {"lines": log_tail("agent", 300)}

        @r("GET", "/api/network")
        def network(req):
            need_login(req)
            p = self.cfg["panel"]
            return {"hostname": hostname(), "ips": local_ips(), "port": p["port"], "bind": p["bind"],
                    "servers": [lk.public() for lk in self.links.values()],
                    "cameras": [{"id": c.id, "name": c.name, "bitrate": c.bitrate(), "online": c.running}
                                for c in self.cameras.values()]}

        @web.websocket("/api/live/{cid}")
        async def live(req, sock):
            if not req.session:
                await sock.send_json({"type": "error", "message": "Login required"})
                return
            cam = self.cameras.get(req.params["cid"])
            if cam is None:
                await sock.send_json({"type": "error", "message": "Camera not found"})
                return
            await self._stream(cam, sock)

    async def _stream(self, cam, sock) -> None:
        viewer = LiveViewer()
        await sock.send_json(meta(cam))
        cam.add(viewer)
        if cam.init:
            viewer.on_init(cam, cam.init)

        async def drain_input():
            try:
                while True:
                    await sock.recv()
            except WebSocketClosed:
                return
        reader = asyncio.ensure_future(drain_input())
        try:
            while not reader.done():
                getter = asyncio.ensure_future(viewer.q.get())
                done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    break
                kind, data = getter.result()
                if kind == 0:
                    await sock.send_text(data)
                else:
                    await sock.send_bytes(bytes([kind]) + data)
        finally:
            cam.remove(viewer)
            reader.cancel()

    # -- helpers -----------------------------------------------------------------------
    def _device(self, key: str):
        if not key:
            return None
        dev = devices.find_device(self._devices_cache.get("video", []), key)
        if dev is None:
            ffmpeg = find_ffmpeg(self.cfg.get("ffmpeg", ""))
            self._devices_cache = devices.list_devices(ffmpeg, True)
            dev = devices.find_device(self._devices_cache["video"], key)
        return dev

    def _audio(self, key: str):
        if not key:
            return None
        return devices.find_device(self._devices_cache.get("audio", []), key)

    def _camera_cfg(self, cid: str) -> dict:
        for c in self.cfg["cameras"]:
            if c["id"] == cid:
                return c
        raise HttpError(404, "Camera not found")

    def _server_cfg(self, sid: str) -> dict:
        for s in self.cfg["servers"]:
            if s["id"] == sid:
                return s
        raise HttpError(404, "Server not found")

    @staticmethod
    def _apply_camera(cam: dict, d: dict, modes: list) -> None:
        if "name" in d:
            cam["name"] = valid_name(d["name"]) or cam["name"]
        for key in ("enabled", "record", "mirror", "timestamp"):
            if key in d:
                cam[key] = bool(d[key])
        if d.get("size"):
            if d["size"] == "auto":
                cam["width"] = cam["height"] = 0
            else:
                try:
                    w, h = (int(x) for x in str(d["size"]).lower().split("x"))
                    cam["width"], cam["height"] = w, h
                except ValueError:
                    raise HttpError(400, "Bad resolution")
        if "fps" in d:
            cam["fps"] = as_int(d["fps"], 15, 1, 60)
        if d.get("quality") in QUALITIES:
            cam["quality"] = d["quality"]
        if "rotate" in d:
            cam["rotate"] = as_int(d["rotate"], 0, 0, 270) // 90 * 90
        if "bitrate" in d:
            cam["bitrate"] = as_int(d["bitrate"], 0, 0, 20000)
        if "input_format" in d and d["input_format"]:
            cam["input_format"] = str(d["input_format"])
        elif "size" in d or "fps" in d:
            fmt, fps = devices.pick_input(modes, cam["width"], cam["height"], cam["fps"])
            cam["input_format"], cam["fps"] = fmt, fps or cam["fps"]

    @staticmethod
    def _apply_server(entry: dict, d: dict, new: bool) -> None:
        if "address" in d:
            addr = str(d["address"]).strip()
            for prefix in ("http://", "https://", "ws://", "wss://"):
                if addr.lower().startswith(prefix):
                    addr = addr[len(prefix):]
            addr = addr.split("/")[0]
            if addr.count(":") == 1 and not d.get("port"):
                addr, port = addr.split(":")
                d["port"] = port
            entry["address"] = addr
        if "port" in d:
            entry["port"] = as_int(d["port"], 0, 0, 65535)
        if "username" in d:
            entry["username"] = str(d["username"]).strip()
        if d.get("password") or new:
            entry["password"] = str(d.get("password", ""))
        if d.get("tls_mode") in TLS_MODES:
            entry["tls_mode"] = d["tls_mode"]
        elif "tls" in d:
            entry["tls_mode"] = "on" if d["tls"] else "auto"
        if d.get("tls_verify") in ("pin", "ca", "none"):
            entry["tls_verify"] = d["tls_verify"]
        if "tls_pin" in d:
            entry["tls_pin"] = str(d["tls_pin"] or "").strip().lower().replace(":", "")
        if "enabled" in d:
            entry["enabled"] = bool(d["enabled"])
        if "cameras" in d:
            entry["cameras"] = "all" if d["cameras"] == "all" else [str(x) for x in (d["cameras"] or [])]
        if not entry["address"]:
            raise HttpError(400, "Enter the server IP address or domain")
        if not entry["port"]:
            raise HttpError(400, "Enter the server port")
        if not entry["username"] or not entry["password"]:
            raise HttpError(400, "Enter the username and password from the server setup")


def run_agent() -> None:
    agent = Agent()
    if not agent.cfg["panel"].get("secret"):
        log.error("The camera panel has no password yet. Run 'eris-cctv' and set this system up as a camera.")
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
