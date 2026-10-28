#!/usr/bin/env bash
# Eris CCTV installer for Linux - https://github.com/eris4444/eris-cctv
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/eris4444/eris-cctv/main/install.sh)
#
# Installs Python 3 (if missing) and Eris CCTV into /opt/eris-cctv, creates the
# `eris-cctv` command and opens the setup menu. Extra arguments are passed to
# eris-cctv, e.g.:  bash install.sh setup-server --port 8800 ...
set -euo pipefail

REPO="eris4444/eris-cctv"
BRANCH="${ERIS_CCTV_BRANCH:-main}"
INSTALL_DIR="${ERIS_CCTV_DIR:-/opt/eris-cctv}"
BIN="/usr/local/bin/eris-cctv"
RAW_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh"

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else
  C_OK=""; C_ERR=""; C_DIM=""; C_B=""; C_0=""
fi
say()  { printf '%s\n' "  $*"; }
ok()   { printf '%s\n' "  ${C_OK}[ok]${C_0} $*"; }
die()  { printf '%s\n' "  ${C_ERR}[x]${C_0} $*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "This installer is for Linux. On Windows run install.ps1 (see README)."

# ---- root -------------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "Please run this as root."
  if [ -f "$0" ] && [ -r "$0" ] && [ "${0#/dev/fd/}" = "$0" ] && [ "${0#/proc/}" = "$0" ]; then
    exec sudo -E bash "$0" "$@"
  fi
  # started through a pipe or <(...): fetch a copy of this script for sudo
  tmp_self="$(mktemp /tmp/eris-cctv-install.XXXXXX)"
  curl -fsSL "$RAW_URL" -o "$tmp_self" || die "Could not download the installer."
  exec sudo -E bash "$tmp_self" "$@"
fi

printf '\n%s\n' "  ${C_B}Eris CCTV installer${C_0}"

# ---- packages ---------------------------------------------------------------------------
PM=""
for p in apt-get dnf yum pacman zypper apk; do
  if command -v "$p" >/dev/null 2>&1; then PM="$p"; break; fi
done

pkg_install() {
  case "$PM" in
    apt-get) DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf)     dnf install -y -q "$@" ;;
    yum)     yum install -y -q "$@" ;;
    pacman)  pacman -Sy --noconfirm --needed "$@" ;;
    zypper)  zypper --non-interactive install "$@" ;;
    apk)     apk add --no-cache "$@" ;;
    *)       return 1 ;;
  esac
}

python_ok() {
  command -v python3 >/dev/null 2>&1 && python3 -c 'import sys, ssl, lzma; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null
}

need=()
python_ok || need+=(python3)
command -v curl >/dev/null 2>&1 || need+=(curl)
command -v tar >/dev/null 2>&1 || need+=(tar)
if [ ${#need[@]} -gt 0 ]; then
  [ -n "$PM" ] || die "Please install: ${need[*]}"
  say "Installing ${need[*]} ..."
  [ "$PM" = "apt-get" ] && apt-get update -qq
  pkgs=("${need[@]}")
  [ "$PM" = "apk" ] && pkgs+=(ca-certificates)
  pkg_install "${pkgs[@]}" ca-certificates || die "Could not install ${need[*]}"
fi
python_ok || die "Python 3.8 or newer is required (found: $(python3 --version 2>&1 || echo none))."
ok "$(python3 --version 2>&1)"

# ---- files ------------------------------------------------------------------------------
work="$(mktemp -d /tmp/eris-cctv.XXXXXX)"
trap 'rm -rf "$work"' EXIT
if [ -n "${ERIS_CCTV_SRC:-}" ]; then
  src="$ERIS_CCTV_SRC"            # local checkout (used by CI)
else
  say "Downloading Eris CCTV (${BRANCH}) from GitHub ..."
  curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}" -o "$work/src.tar.gz" \
    || die "Download failed - check the internet connection / DNS."
  tar -xzf "$work/src.tar.gz" -C "$work"
  src="$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
fi
[ -f "$src/run.py" ] && [ -d "$src/eriscctv" ] || die "Downloaded files look wrong."

mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/eriscctv.new"
cp -r "$src/eriscctv" "$INSTALL_DIR/eriscctv.new"
rm -rf "$INSTALL_DIR/eriscctv"
mv "$INSTALL_DIR/eriscctv.new" "$INSTALL_DIR/eriscctv"
for f in run.py install.sh README.md LICENSE; do
  [ -f "$src/$f" ] && cp "$src/$f" "$INSTALL_DIR/$f"
done
find "$INSTALL_DIR/eriscctv" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod 755 "$INSTALL_DIR" "$INSTALL_DIR/run.py"

PY="$(command -v python3)"
cat > "$BIN" <<EOF
#!/bin/sh
exec "$PY" "$INSTALL_DIR/run.py" "\$@"
EOF
chmod 755 "$BIN"
ok "Installed to $INSTALL_DIR  (command: eris-cctv)"

# restart running services so an update takes effect
for unit in eris-cctv-agent eris-cctv-server; do
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$unit" 2>/dev/null; then
    systemctl restart "$unit" && ok "Restarted $unit"
  fi
done

# ---- run ------------------------------------------------------------------------------------
if [ $# -gt 0 ]; then
  exec "$PY" "$INSTALL_DIR/run.py" "$@"
fi
if [ -t 0 ]; then
  exec "$PY" "$INSTALL_DIR/run.py"
elif [ -r /dev/tty ]; then
  exec "$PY" "$INSTALL_DIR/run.py" </dev/tty
else
  say "Run ${C_B}eris-cctv${C_0} to open the setup menu."
fi
