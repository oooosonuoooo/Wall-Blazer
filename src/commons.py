import os
import subprocess
import sys

LOGGER_NAME = "Wall Blazer"

PROJECT = "com.wallblazer.WallBlazer"
DBUS_NAME_SERVER = f"{PROJECT}.server"
DBUS_NAME_PLAYER = f"{PROJECT}.player"

HOME = os.environ.get("HOME") or os.environ.get("USERPROFILE", "~")
if sys.platform == "win32":
    VIDEO_WALLPAPER_DIR = os.path.join(HOME, "Videos", "Wall Blazer")
    xdg_config_home = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
else:
    try:
        xdg_video_dir = subprocess.check_output(
            "xdg-user-dir VIDEOS", shell=True, encoding="UTF-8"
        ).replace("\n", "")
        # If xdg-user-dir returns HOME itself it means no Videos folder is configured;
        # fall back to ~/Videos/Wall Blazer which is the standard XDG default.
        if os.path.realpath(xdg_video_dir) == os.path.realpath(HOME):
            xdg_video_dir = os.path.join(HOME, "Videos")
        VIDEO_WALLPAPER_DIR = os.path.join(xdg_video_dir, "Wall Blazer")
    except (FileNotFoundError, subprocess.CalledProcessError):
        # xdg-user-dir not found, use $HOME/Videos/Wall Blazer for Video directory instead
        VIDEO_WALLPAPER_DIR = os.path.join(HOME, "Videos", "Wall Blazer")

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config"))
AUTOSTART_DIR = os.path.join(xdg_config_home, "autostart")
AUTOSTART_DESKTOP_PATH = os.path.join(AUTOSTART_DIR, f"{PROJECT}.desktop")
LOCAL_APPLICATIONS_DIR = os.path.join(HOME, ".local", "share", "applications")
LOCAL_APPLICATION_DESKTOP_PATH = os.path.join(LOCAL_APPLICATIONS_DIR, f"{PROJECT}.desktop")
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

CONFIG_VERSION = 11
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
# Reverse playback controls
CONFIG_KEY_REVERSE_SINGLE = "reverse_single_wallpaper"
CONFIG_KEY_REVERSE_PLAYLIST = "reverse_playlist"
CONFIG_KEY_REVERSE_PLAYLIST_ITEMS = "reverse_playlist_items"
# Per-monitor playlists: {monitor_name: [video_path, ...]}
CONFIG_KEY_MONITOR_PLAYLISTS = "monitor_playlists"
# Per-monitor playlist assignment: {monitor_name: playlist_name}
CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS = "monitor_playlist_assignments"
CONFIG_KEY_VIDEO_FIT_MODE = "video_fit_mode"
CONFIG_KEY_PLAYBACK_SPEED_SINGLE = "playback_speed_single"
CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST = "playback_speed_playlist"
CONFIG_KEY_PLAYBACK_SPEED_REVERSE = "playback_speed_reverse"
CONFIG_KEY_VIDEO_ADJUSTMENTS = "video_adjustments"
REVERSE_CACHE_VERSION = 3
# GTK theme
CONFIG_KEY_THEME = "gtk_theme"  # values: "system" | "dark" | "light"
DEFAULT_VIDEO_ADJUSTMENTS = {
    "brightness": 1.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "gamma": 1.0,
    "hue": 0.0,
    "red": 0.0,
    "green": 0.0,
    "blue": 0.0,
    "yellow": 0.0,
    "cyan": 0.0,
    "magenta": 0.0,
}
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
    CONFIG_KEY_REVERSE_SINGLE: False,
    CONFIG_KEY_REVERSE_PLAYLIST: False,
    CONFIG_KEY_REVERSE_PLAYLIST_ITEMS: {},
    CONFIG_KEY_MONITOR_PLAYLISTS: {},  # filled after monitor detection below
    CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS: {},  # filled after monitor detection below
    CONFIG_KEY_VIDEO_FIT_MODE: "cover",
    CONFIG_KEY_PLAYBACK_SPEED_SINGLE: 1.0,
    CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST: 1.0,
    CONFIG_KEY_PLAYBACK_SPEED_REVERSE: 1.0,
    CONFIG_KEY_VIDEO_ADJUSTMENTS: dict(DEFAULT_VIDEO_ADJUSTMENTS),
    CONFIG_KEY_THEME: "system",
}

try:
    from monitor import MonitorInfo
except (ModuleNotFoundError, ImportError):
    from wallblazer.monitor import MonitorInfo

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
