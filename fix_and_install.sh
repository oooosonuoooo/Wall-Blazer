#!/usr/bin/env bash
# Wall Blazer Black Screen Fix - Fast reinstall
# Run with:  bash fix_and_install.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/src"
INSTALL_DIR="/usr/share/wallblazer/wallblazer"

echo "=== Wall Blazer Black Screen Fix ==="
echo ""
echo "This will copy the patched player files to $INSTALL_DIR"
echo "and rebuild/reinstall using meson+ninja."
echo ""

# Option 1: Fast copy (if already installed and we just need to update files)
if [ -d "$INSTALL_DIR/player" ]; then
    echo ">> Fast-installing patched files into $INSTALL_DIR ..."
    sudo cp "$SRC/player/gst_video_player.py"  "$INSTALL_DIR/player/gst_video_player.py"
    sudo cp "$SRC/player/video_player.py"       "$INSTALL_DIR/player/video_player.py"
    sudo cp "$SRC/player/base_player.py"        "$INSTALL_DIR/player/base_player.py"
    echo ">> Files updated. Verifying..."
    python3 -c "
import sys
sys.path.insert(0, '/usr/share/wallblazer')
from wallblazer.player import gst_video_player
print('  OK: gst_video_player imported successfully')
" && echo ">> Syntax check passed!" || {
    echo "!! Syntax error detected in patched files."
    exit 1
}
    echo ""
    echo "=== SUCCESS ==="
    echo "Restart Wall Blazer now:"
    echo "  pkill -f wallblazer || true"
    echo "  wallblazer &"
    exit 0
fi

# Option 2: Full meson build
echo ">> Running full meson build + install..."
rm -rf "$SCRIPT_DIR/build"
meson setup "$SCRIPT_DIR/build" --prefix=/usr
ninja -C "$SCRIPT_DIR/build"
sudo ninja -C "$SCRIPT_DIR/build" install

echo ""
echo "=== SUCCESS ==="
echo "Restart Wall Blazer:"
echo "  pkill -f wallblazer || true"
echo "  wallblazer &"
