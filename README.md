# Eris CCTV

Turn ordinary USB / built-in webcams into a CCTV system. Each computer with webcams runs the **camera
agent**; one or more **servers** collect the cameras and show them in a password-protected web panel.
Camera computers connect **out** to the servers, so they need no public IP, no port forwarding and
no VPN.

```
 camera PC #1 (webcams + mics) ──┐ outgoing connection          ┌── browser / phone
 camera PC #2 ───────────────────┼─────────────────────► SERVER ┼── browser
 camera PC #3 ──────┐            │  (address, port,       :8800 └── ...
                    └────────────┼─► SERVER #2  user, password)
                                 ...
```

* Any number of camera systems can connect to a server, and every camera system can send to any
  number of servers at the same time.
* Video is only sent to a server while somebody is watching there, which saves bandwidth.

## Features

**Camera system (agent)**
- Detects the connected webcams and their microphones (Linux V4L2/ALSA, Windows DirectShow).
- Pick webcams by number (`1,3` or `all`); for webcams that have a built-in microphone you choose
  whether their sound is streamed too.
- H.264 + AAC, adjustable resolution, frame rate, quality, rotation, mirror and a date/time overlay.
- Local English web panel (default port **8090**): live view, cameras, servers, storage, recordings,
  network, settings, logs.
- Recording: choose **which drive** is used (system disk, second disk, USB disk; on Linux unmounted
  disks can be mounted from the panel) or turn recording off. Files rotate every N minutes, old files
  are deleted automatically (keep-for-days, keep-free-space, maximum size). Play, download and delete
  recordings in the panel.
- Keeps working when a webcam is unplugged or a server is unreachable (automatic retries); if a
  microphone fails, video keeps streaming and audio is retried.

**Server**
- English web panel (default port **8800**): live grid of all cameras (1 / 2x2 / 3x3 / 4x4 / auto),
  filter by camera system, audio on/off, snapshot, full screen.
- Camera accounts (the username/password camera systems log in with), panel users (administrator or
  viewer), and per-user camera permissions.
- Optional HTTPS: self-signed certificate (created for you) or your own certificate files.

**Security**
- Passwords are stored only as PBKDF2 hashes.
- Camera systems log in with a challenge/response (HMAC) - the password never crosses the network,
  even without TLS - and the server has to prove it knows the password too.
- With TLS the camera system pins the server certificate on first contact (trust on first use).
- Login throttling, HttpOnly + SameSite cookies, CSRF header check, strict Content-Security-Policy.

## Install

### Linux (Ubuntu, Debian, Raspberry Pi OS, Fedora, ...)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/eris4444/eris-cctv/main/install.sh)
```

### Windows 10 / 11 (PowerShell)

```powershell
irm https://raw.githubusercontent.com/eris4444/eris-cctv/main/install.ps1 | iex
```

The installer copies Eris CCTV to `/opt/eris-cctv` (Linux) or `%LOCALAPPDATA%\ErisCCTV` (Windows),
adds the `eris-cctv` command and opens this menu:

```
   1) This system is a CAMERA   - stream & record its webcams
   2) This system is a SERVER   - central panel to watch all cameras
   3) Camera: choose webcams / microphones
   4) Camera: connect to servers
   5) Server: accounts, users and port
   6) Status
   7) Restart
   8) Logs
   9) Update Eris CCTV
  10) Uninstall
   0) Exit
```

Run `eris-cctv` (Linux: `sudo eris-cctv`) any time to get back to it.

## Quick start

### 1. Set up the server

On the server (for example a VPS) choose **2) This system is a SERVER**. You are asked for:

| Question | Example |
|---|---|
| Port for the web panel and camera connections | `8800` |
| Camera account username / password | `camera` / `********` |
| Use the same login for the web panel? | yes, or a separate admin user |
| Public address (IP or domain) | detected automatically |
| TLS | off / self-signed / own certificate |

At the end it prints the panel address and the details to enter on the camera systems. On Linux the
port is opened in `ufw`/`firewalld` automatically; open it in your VPS provider's firewall too.

### 2. Set up each camera system

On every computer with webcams choose **1) This system is a CAMERA**:

```
   #  Webcam                            Built-in microphone
   1) USB2.0 HD UVC WebCam               -
   2) HK 5M CAM                          Microphone (HK 5M CAM)

  Which webcams? Numbers separated by commas (e.g. 1,2) or 'all' [1]: 1,2
  "HK 5M CAM" has a microphone (Microphone (HK 5M CAM)). Send its sound too? [Y/n]: y
```

Then set the local panel password, add the server (address, port, username, password - the
connection is tested immediately) and optionally pick a drive for recordings. The agent starts in the
background and starts again after a reboot.

### 3. Watch

Open `http://SERVER-ADDRESS:8800` and sign in. To send the same cameras to another server, add it
under **Servers** in the camera panel (or menu option 4).

## Commands

```
eris-cctv                 interactive menu
eris-cctv status          what is configured and running
eris-cctv restart|start|stop
eris-cctv logs [agent|server]
eris-cctv devices         list detected webcams and microphones (JSON)
eris-cctv update          download the latest version from GitHub and restart
eris-cctv uninstall
```

Non-interactive setup (for scripts):

```bash
sudo eris-cctv setup-server --port 8800 --camera-user camera --camera-password 'S3cret!' \
     --admin-user admin --admin-password 'AdminPass' --address 203.0.113.10 --tls self-signed

sudo eris-cctv setup-camera --cameras 1,2 --audio yes --panel-password 'PanelPass' \
     --server 203.0.113.10:8800 --server-user camera --server-password 'S3cret!' [--server-tls] \
     [--record-path /mnt/disk/ErisCCTV]
```

## Requirements

- **Python 3.8+** (installed by the installer if missing). No pip packages are needed.
- **ffmpeg with libx264** on camera systems - installed from the system packages, or a ready-made
  build is downloaded from GitHub (BtbN FFmpeg-Builds) when needed. Servers do not need ffmpeg.
- Linux services use **systemd**. On Windows the camera agent starts when you sign in (a webcam
  needs a signed-in desktop session on Windows); for an always-on Windows camera PC enable automatic
  sign-in.

## Files

| | Linux (root) | Windows |
|---|---|---|
| Program | `/opt/eris-cctv` | `%LOCALAPPDATA%\ErisCCTV\app` |
| Settings | `/etc/eris-cctv/agent.json`, `server.json` | `%LOCALAPPDATA%\ErisCCTV\config` |
| Data / logs | `/var/lib/eris-cctv`, `/var/log/eris-cctv` | `%LOCALAPPDATA%\ErisCCTV\data`, `logs` |
| Recordings | `<drive>/ErisCCTV/<camera>/<YYYY-MM-DD>/<HH-MM-SS>.mp4` | same |
| Services | `eris-cctv-agent`, `eris-cctv-server` (systemd) | autostart entries in `HKCU\...\Run` |

## Troubleshooting

- **No webcam found (Linux):** check `ls /dev/video*`; the agent runs as root so permissions are not
  an issue. Raspberry Pi ribbon cameras need `libcamera`'s V4L2 compatibility layer; USB webcams work
  directly.
- **Camera shows an error:** open *Cameras -> Log* in the camera panel to see the ffmpeg messages.
  Lower the resolution or frame rate if the webcam does not support the chosen mode.
- **Camera system cannot reach the server:** the camera panel's *Servers* page shows the exact
  reason (wrong password, connection refused, timeout, TLS problem). Make sure the server port is
  open in every firewall on the way.
- **Windows firewall prompt:** allow Python on private networks if other devices should open the
  camera panel.
- **Server behind Cloudflare / a reverse proxy:** proxy both `/` and the WebSocket paths
  (`/agent/ws`, `/api/view/...`); WebSocket support must be enabled.

## Development

```bash
python tests/e2e_test.py      # server + camera (test pattern) + viewers + recording, one process
python tests/wizard_test.py   # drives the setup menus with scripted answers
python tests/dev_run.py       # dev server on :18800 (admin/adminpass) and camera panel on :18090
```

CI runs the tests on Python 3.8 and 3.12, a full `install.sh` install with systemd and TLS, and a
simulated USB webcam (v4l2loopback) with an ALSA microphone.

## License

MIT
