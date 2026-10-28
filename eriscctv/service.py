"""Run the agent / server in the background: systemd on Linux, per-user autostart on Windows."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from .util import IS_LINUX, IS_WINDOWS, app_root, read_pid

NAMES = {"agent": "eris-cctv-agent", "server": "eris-cctv-server"}
TITLES = {"agent": "Eris CCTV camera agent", "server": "Eris CCTV server"}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launcher() -> str:
    return os.path.join(app_root(), "run.py")


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl"] + list(args), capture_output=True, text=True)


def has_systemd() -> bool:
    return IS_LINUX and os.path.isdir("/run/systemd/system")


def unit_path(role: str) -> str:
    return "/etc/systemd/system/%s.service" % NAMES[role]


def pythonw() -> str:
    exe = sys.executable
    if IS_WINDOWS:
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return exe


# ------------------------------------------------------------------------------ install
def install(role: str) -> str:
    if os.environ.get("ERIS_CCTV_NO_SERVICE"):  # tests: never touch the real system
        return "service skipped (ERIS_CCTV_NO_SERVICE)"
    if IS_WINDOWS:
        import winreg
        cmd = '"%s" "%s" %s run' % (pythonw(), launcher(), role)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, TITLES[role], 0, winreg.REG_SZ, cmd)
        _windows_firewall(role)
        restart(role)
        return "autostart at sign-in"
    if not has_systemd():
        raise RuntimeError("systemd was not found; start it manually with: python3 %s %s run" % (launcher(), role))
    after = "network-online.target sound.target" if role == "agent" else "network-online.target"
    unit = "\n".join([
        "[Unit]",
        "Description=%s" % TITLES[role],
        "After=%s" % after,
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=%s %s %s run" % (sys.executable, launcher(), role),
        "Restart=always",
        "RestartSec=3",
        "KillMode=mixed",
        "TimeoutStopSec=60",
        "Environment=PYTHONUNBUFFERED=1",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        ""])
    with open(unit_path(role), "w") as f:
        f.write(unit)
    _systemctl("daemon-reload")
    _systemctl("enable", NAMES[role])
    r = _systemctl("restart", NAMES[role])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "systemctl restart failed")
    return "systemd service %s" % NAMES[role]


def uninstall(role: str) -> None:
    stop(role)
    if IS_WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, TITLES[role])
        except OSError:
            pass
        return
    if has_systemd():
        _systemctl("disable", NAMES[role])
        try:
            os.remove(unit_path(role))
        except OSError:
            pass
        _systemctl("daemon-reload")


def installed(role: str) -> bool:
    if IS_WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, TITLES[role])
            return True
        except OSError:
            return False
    return os.path.exists(unit_path(role))


# ------------------------------------------------------------------------------ control
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running(role: str) -> bool:
    if has_systemd() and installed(role):
        return _systemctl("is-active", NAMES[role]).stdout.strip() == "active"
    return _pid_alive(read_pid(role))


def start(role: str) -> None:
    if running(role):
        return
    if has_systemd() and installed(role):
        _systemctl("start", NAMES[role])
        return
    flags = 0
    if IS_WINDOWS:
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    subprocess.Popen([pythonw(), launcher(), role, "run"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
                     start_new_session=not IS_WINDOWS)
    time.sleep(1.5)


def stop(role: str) -> None:
    if has_systemd() and installed(role):
        _systemctl("stop", NAMES[role])
        return
    pid = read_pid(role)
    if not _pid_alive(pid):
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        for _ in range(60):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
    time.sleep(0.5)


def restart(role: str) -> None:
    if os.environ.get("ERIS_CCTV_NO_SERVICE"):
        return
    if has_systemd() and installed(role):
        _systemctl("restart", NAMES[role])
        return
    stop(role)
    start(role)


def _windows_firewall(role: str) -> None:
    """Best effort: allow python through the Windows firewall (needs admin; silently skipped otherwise)."""
    name = "Eris CCTV (%s)" % role
    try:
        subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + name],
                       capture_output=True, timeout=15)
        subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", "name=" + name, "dir=in",
                        "action=allow", "program=" + pythonw(), "enable=yes"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def open_linux_firewall(port: int) -> str:
    """Opens a TCP port in ufw / firewalld when they are active."""
    import shutil
    msgs = []
    if shutil.which("ufw"):
        st = subprocess.run(["ufw", "status"], capture_output=True, text=True).stdout
        if "Status: active" in st:
            subprocess.run(["ufw", "allow", "%d/tcp" % port], capture_output=True)
            msgs.append("ufw: allowed %d/tcp" % port)
    if shutil.which("firewall-cmd"):
        st = subprocess.run(["firewall-cmd", "--state"], capture_output=True, text=True).stdout.strip()
        if st == "running":
            subprocess.run(["firewall-cmd", "--permanent", "--add-port=%d/tcp" % port], capture_output=True)
            subprocess.run(["firewall-cmd", "--reload"], capture_output=True)
            msgs.append("firewalld: opened %d/tcp" % port)
    return "; ".join(msgs)
