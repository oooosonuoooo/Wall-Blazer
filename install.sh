#!/usr/bin/env bash

set -euo pipefail

MODE="install"
ENABLE_AUTOSTART=false
INSTALL_DEPENDENCIES=true
for arg in "$@"; do
  case "$arg" in
    --repair)
      MODE="repair"
      ;;
    --enable-autostart)
      ENABLE_AUTOSTART=true
      ;;
    --skip-dependencies)
      INSTALL_DEPENDENCIES=false
      ;;
    *)
      echo "Usage: $0 [--repair] [--enable-autostart] [--skip-dependencies]" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
AUTOREPAIR_SERVICE_DIR="$HOME/.config/systemd/user"
AUTOREPAIR_SERVICE="$AUTOREPAIR_SERVICE_DIR/wallblazer-autorepair.service"
AUTOREPAIR_TIMER="$AUTOREPAIR_SERVICE_DIR/wallblazer-autorepair.timer"
INSTALL_STATE_DIR="/usr/local/share/wallblazer"
SOURCE_TRACK_FILE="$INSTALL_STATE_DIR/source-path"

echo "==========================================="
echo "   Wall Blazer Installer & Auto-Repair     "
echo "==========================================="

if [[ "$MODE" == "repair" ]]; then
  echo ">> Running in repair mode"
fi

if [[ "$EUID" -eq 0 ]]; then
  echo "Please do not run this script as root directly."
  echo "It will ask for sudo password when necessary."
  exit 1
fi

backup_if_exists() {
  local target="$1"
  if [[ -e "$target" ]]; then
    local ts backup
    ts="$(date +%Y%m%d-%H%M%S)"
    backup="${target}.user-backup-${ts}"
    mv "$target" "$backup"
    echo ">> Backed up conflicting local install: $target -> $backup"
  fi
}

apt_install_optional() {
  local pkg
  for pkg in "$@"; do
    if ! sudo apt-get install -y "$pkg" >/dev/null 2>&1; then
      echo ">> Optional package '$pkg' unavailable on this distro. Skipping."
    fi
  done
}

detect_package_manager() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
  elif command -v dnf >/dev/null 2>&1; then
    echo "dnf"
  elif command -v pacman >/dev/null 2>&1; then
    echo "pacman"
  elif command -v zypper >/dev/null 2>&1; then
    echo "zypper"
  else
    echo ""
  fi
}

install_system_dependencies() {
  echo ">> Installing system dependencies (requires sudo)..."
  local pm
  pm="$(detect_package_manager)"

  case "$pm" in
    apt)
      if ! sudo apt-get update; then
        echo "!! apt-get update failed. Continuing with existing package indexes..."
      fi
      sudo apt-get install -y \
        python3 python3-pip python3-gi python3-gi-cairo \
        gir1.2-gtk-3.0 gir1.2-wnck-3.0 gir1.2-ayatanaappindicator3-0.1 \
        python3-gst-1.0 gstreamer1.0-tools gstreamer1.0-gl gstreamer1.0-gtk3 \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
        meson ninja-build pkg-config vlc ffmpeg gettext vdpauinfo \
        python3-pydbus python3-pil python3-vlc python3-setproctitle \
        python3-requests yt-dlp mesa-utils
      apt_install_optional \
        vainfo va-driver-all mesa-va-drivers mesa-vdpau-drivers \
        vdpau-driver-all intel-media-va-driver i965-va-driver \
        libva2 libva-drm2 libva-x11-2 libvdpau1
      ;;
    dnf)
      sudo dnf install -y \
        python3 python3-pip python3-gobject gtk3 libwnck3 libayatana-appindicator-gtk3 \
        meson ninja-build pkgconf-pkg-config vlc ffmpeg gettext vdpauinfo \
        python3-pydbus python3-pillow python3-vlc python3-setproctitle \
        python3-requests yt-dlp mesa-dri-drivers
      sudo dnf install -y libva-utils mesa-vdpau-drivers || true
      ;;
    pacman)
      sudo pacman -Syu --noconfirm
      sudo pacman -S --needed --noconfirm \
        python python-pip python-gobject gtk3 libwnck3 libayatana-appindicator \
        meson ninja pkgconf vlc ffmpeg gettext vdpauinfo \
        python-pydbus python-pillow python-vlc python-setproctitle \
        python-requests yt-dlp mesa-utils libva-utils
      ;;
    zypper)
      sudo zypper --non-interactive refresh || true
      sudo zypper --non-interactive install \
        python3 python3-pip python3-gobject python3-cairo \
        typelib-1_0-Gtk-3_0 typelib-1_0-Wnck-3_0 \
        meson ninja pkg-config vlc ffmpeg gettext-tools vdpauinfo \
        python3-pydbus python3-Pillow python3-python-vlc python3-setproctitle \
        python3-requests yt-dlp libva-utils
      ;;
    *)
      echo "!! Unsupported Linux package manager. Install GTK3, VLC, FFmpeg, Meson, Ninja, yt-dlp,"
      echo "   and the Python dependencies from requirements.txt manually, then rerun install.sh."
      exit 1
      ;;
  esac
}

verify_python_dependencies() {
  echo ">> Verifying Python dependencies..."
  if python3 - <<'PY'
import importlib.util
import sys

required = [
    ("pydbus", "pydbus"),
    ("PIL", "Pillow"),
    ("vlc", "python-vlc"),
    ("yt_dlp", "yt-dlp"),
    ("setproctitle", "setproctitle"),
    ("requests", "requests"),
]
missing = [pkg for module, pkg in required if importlib.util.find_spec(module) is None]
if missing:
    print("Missing modules: " + ", ".join(missing))
    sys.exit(1)
print("All required Python modules are available.")
PY
  then
    return
  fi

  echo ">> Installing missing Python dependencies with pip --user..."
  if python3 -m pip install --help 2>&1 | grep -q -- "--break-system-packages"; then
    python3 -m pip install --user --break-system-packages pydbus Pillow python-vlc yt-dlp setproctitle requests
  else
    python3 -m pip install --user pydbus Pillow python-vlc yt-dlp setproctitle requests
  fi
}

build_and_install() {
  echo ">> Cleaning old build directory..."
  rm -rf "$BUILD_DIR"

  echo ">> Building Wall Blazer with Meson..."
  meson setup "$BUILD_DIR" --prefix=/usr/local

  echo ">> Compiling..."
  ninja -C "$BUILD_DIR"

  echo ">> Installing Wall Blazer system-wide (requires sudo)..."
  sudo ninja -C "$BUILD_DIR" install
}

install_repair_runner() {
  echo ">> Installing wallblazer-repair helper..."
  sudo install -d "$INSTALL_STATE_DIR"
  echo "$SCRIPT_DIR" | sudo tee "$SOURCE_TRACK_FILE" >/dev/null

  tmp_runner="$(mktemp)"
  cat > "$tmp_runner" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SOURCE_TRACK_FILE="/usr/local/share/wallblazer/source-path"

if command -v wallblazer >/dev/null 2>&1; then
  wallblazer --repair || true
fi

if [[ -f "$SOURCE_TRACK_FILE" ]]; then
  SRC_DIR="$(cat "$SOURCE_TRACK_FILE")"
  if [[ -x "$SRC_DIR/install.sh" ]]; then
    exec "$SRC_DIR/install.sh" --repair
  fi
fi

echo "wallblazer-repair: installer source path not found."
echo "Run install.sh from your Wall Blazer source checkout."
exit 1
EOF
  sudo install -m 0755 "$tmp_runner" /usr/local/bin/wallblazer-repair
  rm -f "$tmp_runner"
}

remove_legacy_repair_timer() {
  if [[ ! -f "$AUTOREPAIR_TIMER" && ! -f "$AUTOREPAIR_SERVICE" ]]; then
    return
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now wallblazer-autorepair.timer 2>/dev/null || true
    systemctl --user stop wallblazer-autorepair.service 2>/dev/null || true
  fi
  rm -f "$AUTOREPAIR_SERVICE" "$AUTOREPAIR_TIMER" \
    "$AUTOREPAIR_SERVICE_DIR/timers.target.wants/wallblazer-autorepair.timer"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
  fi
}

if [[ "$MODE" != "repair" ]]; then
  echo ">> Checking for conflicting user-local Wall Blazer install..."
  backup_if_exists "$HOME/.local/bin/wallblazer"
  backup_if_exists "$HOME/.local/share/wallblazer"
fi

if "$INSTALL_DEPENDENCIES"; then
  install_system_dependencies
fi
verify_python_dependencies
build_and_install
install_repair_runner
remove_legacy_repair_timer

echo ">> Running runtime repair verification..."
/usr/local/bin/wallblazer --repair
if "$ENABLE_AUTOSTART"; then
  /usr/local/bin/wallblazer --enable-autostart
fi

echo ">> Setup finished successfully!"
echo "Launch from app menu, or run 'wallblazer'."
echo "For manual repair, run 'wallblazer-repair'."
