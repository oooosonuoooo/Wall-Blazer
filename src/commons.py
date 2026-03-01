import os
import subprocess

LOGGER_NAME = "Wall Blazer"

PROJECT = "com.wallblazer.WallBlazer"
DBUS_NAME_SERVER = f"{PROJECT}.server"
DBUS_NAME_PLAYER = f"{PROJECT}.player"

import sys

HOME = os.environ.get("HOME") or os.environ.get("USERPROFILE", "~")
if sys.platform == "win32":
    VIDEO_WALLPAPER_DIR = os.path.join(HOME, "Videos", "Wall Blazer")
    xdg_config_home = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
else:
    try:
        xdg_video_dir = subprocess.check_output(
            "xdg-user-dir VIDEOS", shell=True, encoding="UTF-8"
        ).replace("\n", "")
        VIDEO_WALLPAPER_DIR = os.path.join(xdg_video_dir, "Wall Blazer")
    except (FileNotFoundError, subprocess.CalledProcessError):
        # xdg-user-dir not found, use $HOME/Wall Blazer for Video directory instead
        VIDEO_WALLPAPER_DIR = os.path.join(HOME, "Wall Blazer")

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config"))
AUTOSTART_DIR = os.path.join(xdg_config_home, "autostart")
AUTOSTART_DESKTOP_PATH = os.path.join(AUTOSTART_DIR, f"{PROJECT}.desktop")
AUTOSTART_DESKTOP_CONTENT = """[Desktop Entry]
Name=Wall Blazer
Exec=wallblazer -b
Icon=com.wallblazer.WallBlazer
Terminal=false
Type=Application
Categories=GTK;Utility;
StartupNotify=true
"""
AUTOSTART_DESKTOP_CONTENT_FLATPAK = """[Desktop Entry]
Name=Wall Blazer
Exec=/usr/bin/flatpak run --command=wallblazer com.wallblazer.WallBlazer -b
Icon=com.wallblazer.WallBlazer
Terminal=false
Type=Application
Categories=GTK;Utility;
StartupNotify=true
X-Flatpak=com.wallblazer.WallBlazer
"""

CONFIG_DIR = os.path.join(xdg_config_home, "wallblazer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

MODE_NULL = "MODE_NULL"
MODE_VIDEO = "MODE_VIDEO"
MODE_STREAM = "MODE_STREAM"
MODE_WEBPAGE = "MODE_WEBPAGE"

CONFIG_VERSION = 9
CONFIG_KEY_VERSION = "version"
CONFIG_KEY_MODE = "mode"
CONFIG_KEY_DATA_SOURCE = "data_source"
CONFIG_KEY_MUTE = "is_mute"
CONFIG_KEY_VOLUME = "audio_volume"
CONFIG_KEY_STATIC_WALLPAPER = "is_static_wallpaper"
CONFIG_KEY_BLUR_RADIUS = "static_wallpaper_blur_radius"
CONFIG_KEY_PAUSE_WHEN_MAXIMIZED = "is_pause_when_maximized"
CONFIG_KEY_MUTE_WHEN_MAXIMIZED = "is_mute_when_maximized"
CONFIG_KEY_FADE_DURATION_SEC = "fade_duration_sec"
CONFIG_KEY_FADE_INTERVAL = "fade_interval"
CONFIG_KEY_SYSTRAY = "is_show_systray"
CONFIG_KEY_FIRST_TIME = "is_first_time"
CONFIG_KEY_PLAYLIST = "playlist_enabled"
CONFIG_KEY_PLAYLIST_INTERVAL = "playlist_interval_sec"
CONFIG_KEY_PLAYLIST_SHUFFLE = "playlist_shuffle"
CONFIG_KEY_PLAYLIST_SELECTION = "playlist_selection"
CONFIG_KEY_PLAYLIST_LIBRARY = "playlist_library"
CONFIG_KEY_PLAYLIST_ACTIVE = "playlist_active"
# Per-monitor playlists: {monitor_name: [video_path, ...]}
CONFIG_KEY_MONITOR_PLAYLISTS = "monitor_playlists"
# Per-monitor playlist assignment: {monitor_name: playlist_name}
CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS = "monitor_playlist_assignments"
# GTK theme
CONFIG_KEY_THEME = "gtk_theme"  # values: "system" | "dark" | "light"
CONFIG_TEMPLATE = {
    CONFIG_KEY_VERSION: CONFIG_VERSION,
    CONFIG_KEY_MODE: MODE_NULL,
    CONFIG_KEY_DATA_SOURCE: None,
    CONFIG_KEY_MUTE: False,
    CONFIG_KEY_VOLUME: 50,
    CONFIG_KEY_STATIC_WALLPAPER: True,
    CONFIG_KEY_BLUR_RADIUS: 5,
    CONFIG_KEY_PAUSE_WHEN_MAXIMIZED: True,
    CONFIG_KEY_MUTE_WHEN_MAXIMIZED: False,
    CONFIG_KEY_FADE_DURATION_SEC: 1.5,
    CONFIG_KEY_FADE_INTERVAL: 0.1,
    CONFIG_KEY_SYSTRAY: False,
    CONFIG_KEY_FIRST_TIME: True,
    CONFIG_KEY_PLAYLIST: False,
    CONFIG_KEY_PLAYLIST_INTERVAL: 300,
    CONFIG_KEY_PLAYLIST_SHUFFLE: False,
    CONFIG_KEY_PLAYLIST_SELECTION: [],
    CONFIG_KEY_PLAYLIST_LIBRARY: {"Default": []},
    CONFIG_KEY_PLAYLIST_ACTIVE: "Default",
    CONFIG_KEY_MONITOR_PLAYLISTS: {},  # filled after monitor detection below
    CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS: {},  # filled after monitor detection below
    CONFIG_KEY_THEME: "system",
}

try:
    from monitor import Monitor, Monitors, MonitorInfo
except (ModuleNotFoundError, ImportError):
    from wallblazer.monitor import Monitor, Monitors, MonitorInfo

# initialize config according to monitors
data_sources = {"Default": ""}
try:
    info = MonitorInfo()
    monitors = info.monitors()
    for monitor in monitors:
        monitor_name = monitor.get("name")
        if isinstance(monitor_name, str) and monitor_name:
            data_sources[monitor_name] = ""
except Exception:
    # Headless/non-graphical contexts should still be able to load config helpers.
    pass

CONFIG_TEMPLATE[CONFIG_KEY_DATA_SOURCE] = data_sources
CONFIG_TEMPLATE[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = {
    monitor_name: "Default" for monitor_name in data_sources
}
