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
- TLS and plain HTTP on the **same port**: camera systems encrypt automatically, browsers can keep
  using `http://` (or `https://`). The certificate is self-signed and created for you (no openssl
  needed), or use your own certificate files.

**Security**
- Passwords are stored only as PBKDF2 hashes.
- Camera systems log in with a challenge/response (HMAC) - the password never crosses the network -
  and the server has to prove it knows the password too.
- The camera -> server link uses TLS automatically and pins the server certificate on first contact
  (trust on first use). Many networks throttle unencrypted uploads, so this also keeps video moving.
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
| Encryption (TLS) | automatic self-signed certificate (recommended) / own certificate / off |

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
connection is tested immediately and encrypted automatically) and optionally pick a drive for
recordings. The agent starts in the background and starts again after a reboot.

### 3. Watch

Open `http://SERVER-ADDRESS:8800` and sign in. To send the same cameras to another server, add it
under **Servers** in the camera panel (or menu option 4).

## Server behind a CDN (Iran and other throttled networks)

Some networks throttle uploads to foreign servers whatever the protocol. Measured from an Iranian home
line in 2026: to a VPS in the UK ~15 KB/s and the connection cut after 20-30 s, even with TLS; to a CDN
edge inside the country 3 MB/s. Then let the camera systems upload to a CDN that has edges inside the
country (for example ArvanCloud) - or Cloudflare - and let the CDN carry the video to the server:

1. **DNS:** add a record for a new name (for example `cctv.example.ir`) with the server's IP and the
   CDN / proxy switched **on**.
2. **Origin port:** the CDN has to reach the server's port (default 8800). Set the record's origin /
   upstream port to `8800` (Cloudflare: an Origin Rule). The server answers HTTP and HTTPS on that port,
   so either upstream protocol works. If the CDN cannot use another port, change the server port to
   `80` or `443` (menu: *Server: accounts, users and port* -> *Change the port*).
3. Turn on **HTTPS** for that name in the CDN, and **WebSocket** support if the CDN has a switch for it.
4. On every camera system (camera panel -> *Servers* -> *Edit*): address `cctv.example.ir`, port `443`.
5. Watch at `https://cctv.example.ir`.

The camera system pins the CDN's certificate and follows its renewals automatically: a new
certificate is accepted when a public CA vouches for it for that name; anything else is refused.

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
     --admin-user admin --admin-password 'AdminPass' --address 203.0.113.10 [--tls auto|files|off]

sudo eris-cctv setup-camera --cameras 1,2 --audio yes --panel-password 'PanelPass' \
     --server 203.0.113.10:8800 --server-user camera --server-password 'S3cret!' \
     [--server-tls auto|on|off] [--record-path /mnt/disk/ErisCCTV]
```

`--server-tls auto` (the default) encrypts whenever the server offers TLS and falls back to an
unencrypted link only for servers that never had TLS; `--server-tls` alone means `on` (TLS required).

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
- **Server panel stays on "Starting video..." and the link reconnects every 20-50 s:** the network
  throttles uploads abroad (seen on Iranian lines: plain uploads crawl at ~5 KB/s and are reset after
  ~20 s). Update **both** the server and the camera system to 1.1 or newer - the link then uses TLS
  automatically; the *Servers* page of the camera panel and the *Camera systems* page of the server
  show "Encrypted" for every link. If an encrypted link still drops every 20-30 s, the network
  throttles that server's address itself: put the server behind a CDN (see above).
- **"The server's TLS certificate changed":** the server was reinstalled or got a new certificate.
  In the camera panel open *Servers -> Edit* and click *Forget pinned certificate*.
- **Slow upload:** when the uplink is slower than the camera, the agent skips stale fragments so the
  picture stays live (a few seconds behind at most). Lower the camera's quality or resolution in
  *Cameras -> Edit* for a smooth picture.
- **Windows firewall prompt:** allow Python on private networks if other devices should open the
  camera panel.
- **Server behind Cloudflare / a reverse proxy:** proxy both `/` and the WebSocket paths
  (`/agent/ws`, `/api/view/...`); WebSocket support must be enabled.

## Development

```bash
python tests/e2e_test.py       # server + camera (test pattern) + viewers + recording, one process
python tests/wizard_test.py    # drives the setup menus with scripted answers
python tests/throttle_test.py  # camera link through a network that throttles unencrypted uploads
python tests/certs_test.py     # self-signed certificate generator + TLS handshake
python tests/dev_run.py        # dev server on :18800 (admin/adminpass) and camera panel on :18090
```

CI runs the tests on Python 3.8 and 3.12, a full `install.sh` install with systemd and TLS, an
upgrade from 1.0 (unencrypted) to the current version, and a simulated USB webcam (v4l2loopback).

## Changes

- **1.1.2** - Works behind a CDN (ArvanCloud, Cloudflare): a pinned certificate that is renewed and
  valid for the name from a public CA is followed automatically. README: CDN setup.
- **1.1.1** - A camera system that fell back to an unencrypted link (server still on 1.0) switches to
  TLS within seconds once that server is updated - update the camera systems and the server in any
  order.
- **1.1.0** - The camera -> server link is encrypted automatically: the server serves TLS and plain
  HTTP on the same port with a self-signed certificate it creates itself, camera systems use TLS
  whenever it is offered (fixes "Starting video..." on networks that throttle unencrypted uploads).
  Quick reconnects, stale video skipped on slow uplinks, link encryption and speed shown in both
  panels.
- **1.0.0** - First release.

## License

MIT
