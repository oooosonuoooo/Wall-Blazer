import json
import logging
import shutil
import subprocess
import pathlib
import glob

import sys
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck
except ValueError:
    pass
from gi.repository import Gio, GLib, Gtk

try:
    import pydbus
except ImportError:
    pydbus = None

try:
    from commons import *
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import *

logger = logging.getLogger(LOGGER_NAME)

VIDEO_FILE_EXTENSIONS = {
    ".3g2", ".3gp", ".asf", ".avi", ".f4v", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".mxf", ".ogg", ".ogm", ".ogv",
    ".qt", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}
REQUIRED_RUNTIME_BINARIES = ["vlc", "ffmpeg", "ffprobe", "yt-dlp"]


def _resolve_thumb_cache_dir():
    preferred = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")),
        "wallblazer",
        "thumbs",
    )
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except OSError:
        fallback = os.path.join("/tmp", "wallblazer-cache", "thumbs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


THUMB_CACHE_DIR = _resolve_thumb_cache_dir()
_GPU_PROFILE_CACHE = None
GPU_VENDOR_MAP = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x1022": "amd",
    "0x8086": "intel",
}


def _run_text_cmd(args, timeout=5):
    try:
        ret = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        if ret.returncode == 0:
            return ret.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _safe_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _redacted_config_summary(config):
    """
    Return a privacy-safe config summary without local paths, URLs, or monitor names.
    """
    if not isinstance(config, dict):
        return {"valid": False}

    data_source = config.get(CONFIG_KEY_DATA_SOURCE, {})
    if not isinstance(data_source, dict):
        data_source = {}
    monitor_count = len(
        [name for name in data_source.keys() if isinstance(name, str) and name != "Default"]
    )

    playlist_library = config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
    if not isinstance(playlist_library, dict):
        playlist_library = {}
    playlist_count = len(playlist_library)
    playlist_item_count = 0
    for items in playlist_library.values():
        if isinstance(items, list):
            playlist_item_count += len(items)

    return {
        "version": config.get(CONFIG_KEY_VERSION),
        "mode": config.get(CONFIG_KEY_MODE),
        "monitor_count": monitor_count,
        "has_default_source": bool(str(data_source.get("Default", "")).strip()),
        "playlist_count": playlist_count,
        "playlist_item_count": playlist_item_count,
        "playlist_enabled": bool(config.get(CONFIG_KEY_PLAYLIST, False)),
        "theme": config.get(CONFIG_KEY_THEME, "system"),
    }


def is_gnome():
    """
    Check if current DE is GNOME or not.
    On Ubuntu 20.04, $XDG_CURRENT_DESKTOP = ubuntu:GNOME
    On Fedora 34, $XDG_CURRENT_DESKTOP = GNOME
    Hence we do the detection by looking for the word "gnome"
    """
    return "gnome" in str(os.environ.get("XDG_CURRENT_DESKTOP") or '').lower()


def is_wayland():
    """
    Check if current session is Wayland or not.
    $XDG_SESSION_TYPE = x11 | wayland
    """
    return os.environ.get("XDG_SESSION_TYPE") == "wayland"


def is_nvidia_proprietary():
    """
    Check if the GPU is nvidia and the driver is proprietary
    """
    if sys.platform == "win32":
        return False
    if shutil.which("glxinfo") is None:
        logger.debug("[Utils] glxinfo not found, skipping GPU vendor check")
        return False
    ret = subprocess.run(["glxinfo", "-B"],
                         stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         text=True)
    if ret.returncode != 0:
        logger.debug(f"[Utils] glxinfo check failed with code={ret.returncode}")
        return False
    output = ret.stdout
    return "OpenGL vendor string: NVIDIA Corporation" in output


def is_vdpau_ok():
    """
    Check if the VDPAU works fine.

    vdpauinfo is optional, so missing binary should not be logged as an error.
    """
    if sys.platform == "win32":
        return False
    if shutil.which("vdpauinfo") is None:
        logger.debug("[Utils] vdpauinfo not found, skipping VDPAU check")
        return False
    ret = subprocess.run(["vdpauinfo"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.STDOUT)
    return ret.returncode == 0


def is_flatpak():
    """
    Check if Wall Blazer is a Flatpak
    Reference:
    https://gitlab.gnome.org/jrb/crosswords/-/blob/master/src/crosswords-init.c#L179
    """
    if sys.platform == "win32":
        return False
    return os.path.isfile('/.flatpak-info')


def detect_gpu_vendors():
    """
    Detect current GPU vendors from lspci/glxinfo best-effort.
    Returns a sorted list, e.g. ['amd', 'intel'].
    """
    vendors = set()

    lspci_output = _run_text_cmd(["lspci", "-nnk"], timeout=4)
    if lspci_output:
        for line in lspci_output.splitlines():
            low = line.lower()
            if (
                "vga compatible controller" not in low
                and "3d controller" not in low
                and "display controller" not in low
            ):
                continue
            for vendor in ("nvidia", "intel", "amd", "ati", "radeon", "vmware", "virtio"):
                if vendor in low:
                    vendors.add("amd" if vendor in {"ati", "radeon"} else vendor)

    glxinfo_output = _run_text_cmd(["glxinfo", "-B"], timeout=4)
    if glxinfo_output:
        low = glxinfo_output.lower()
        if "nvidia corporation" in low:
            vendors.add("nvidia")
        if "intel" in low:
            vendors.add("intel")
        if "amd" in low or "advanced micro devices" in low:
            vendors.add("amd")
        if "llvmpipe" in low or "software rasterizer" in low:
            vendors.add("software")

    return sorted(vendors)


def detect_hw_accel_methods():
    """
    Detect ffmpeg HW acceleration methods available on this system.
    Returns a sorted list, e.g. ['vaapi', 'vdpau'].
    """
    methods = set()
    ffmpeg_output = _run_text_cmd(["ffmpeg", "-hide_banner", "-hwaccels"], timeout=5)
    if ffmpeg_output:
        for line in ffmpeg_output.splitlines():
            item = line.strip().lower()
            if not item or item.startswith("hardware acceleration methods"):
                continue
            if " " in item:
                continue
            methods.add(item)

    if shutil.which("vainfo") is not None:
        ret = subprocess.run(
            ["vainfo"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ret.returncode == 0:
            methods.add("vaapi")

    if "vdpau" in methods and not is_vdpau_ok():
        methods.discard("vdpau")
    elif is_vdpau_ok():
        methods.add("vdpau")

    return sorted(methods)


def get_vlc_hwdec_profile(force_refresh=False):
    """
    Build a VLC hw-decode profile:
    - Prefer GPU decode when available.
    - Fall back to CPU decode (`none`) when no stable hw backend is detected.
    Environment override: WALLBLAZER_FORCE_HWDEC (e.g. any|none|vaapi|vdpau|drm).
    """
    global _GPU_PROFILE_CACHE
    if _GPU_PROFILE_CACHE is not None and not force_refresh:
        return dict(_GPU_PROFILE_CACHE)

    if sys.platform == "win32":
        profile = {
            "hwdec": "any",
            "gpu_available": True,
            "reason": "Windows default HW decode",
            "vendors": ["windows_gpu"],
            "methods": ["d3d11va", "dxva2"],
        }
        _GPU_PROFILE_CACHE = profile
        return dict(profile)

    forced = str(os.environ.get("WALLBLAZER_FORCE_HWDEC", "")).strip().lower()
    if forced:
        profile = {
            "hwdec": forced,
            "gpu_available": forced != "none",
            "reason": f"forced via WALLBLAZER_FORCE_HWDEC={forced}",
            "vendors": detect_gpu_vendors(),
            "methods": detect_hw_accel_methods(),
        }
        _GPU_PROFILE_CACHE = profile
        return dict(profile)

    methods = detect_hw_accel_methods()
    vendors = detect_gpu_vendors()

    hwdec = "none"
    reason = "No HW decoder detected, CPU fallback"
    has_nvidia = "nvidia" in vendors
    has_intel_amd = ("intel" in vendors) or ("amd" in vendors)

    if has_nvidia and has_intel_amd:
        # Hybrid systems can expose multiple backends; let VLC choose the active one.
        if methods:
            hwdec = "any"
            reason = "Hybrid GPU detected; VLC auto-select HW decode backend"
    elif has_nvidia:
        if "vdpau" in methods and is_vdpau_ok():
            hwdec = "vdpau"
            reason = "Detected NVIDIA VDPAU hardware decode path"
        elif methods:
            hwdec = "any"
            reason = "Detected NVIDIA GPU; VLC auto-select HW decode backend"
    else:
        for method in ("vaapi", "drm", "vdpau"):
            if method in methods:
                hwdec = method
                reason = f"Detected {method} hardware decode path"
                break
        else:
            if methods:
                hwdec = "any"
                reason = "Generic HW decode requested (VLC auto-select)"

    if hwdec == "vdpau" and not is_vdpau_ok():
        hwdec = "none"
        reason = "VDPAU probe failed; using CPU fallback"

    profile = {
        "hwdec": hwdec,
        "gpu_available": hwdec != "none",
        "reason": reason,
        "vendors": vendors,
        "methods": methods,
    }
    _GPU_PROFILE_CACHE = profile
    logger.info(
        f"[GPU] vendors={vendors or ['unknown']} methods={methods or ['none']} "
        f"selected_hwdec={hwdec} reason={reason}"
    )
    return dict(profile)


def run_runtime_self_repair(include_gpu_probe=False):
    """
    Best-effort runtime self-repair:
    - recreate runtime directories
    - regenerate/normalize config if broken
    - report missing runtime binaries
    """
    os.makedirs(VIDEO_WALLPAPER_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)

    config = ConfigUtil().load()
    ConfigUtil().save(config)

    missing = [cmd for cmd in REQUIRED_RUNTIME_BINARIES if shutil.which(cmd) is None]
    gpu_profile = get_vlc_hwdec_profile(force_refresh=True) if include_gpu_probe else None
    status = {
        "ok": len(missing) == 0,
        "missing_binaries": missing,
        "gpu_profile": gpu_profile,
        "config_path": CONFIG_PATH,
        "video_dir": VIDEO_WALLPAPER_DIR,
        "thumb_cache_dir": THUMB_CACHE_DIR,
    }
    if missing:
        logger.warning(f"[Repair] Missing runtime binaries: {', '.join(missing)}")
    else:
        logger.info("[Repair] Runtime self-repair completed successfully")
    return status


def purge_local_app_data():
    """
    Remove persisted local media/user-device state from Wall Blazer config and caches.
    Keeps only safe defaults required for the app to run.
    """
    config_util = ConfigUtil()
    config = config_util.load()

    data_source = config.get(CONFIG_KEY_DATA_SOURCE, {})
    if not isinstance(data_source, dict):
        data_source = {}
    sanitized_data_source = {"Default": ""}
    for key in data_source.keys():
        if isinstance(key, str) and key != "Default":
            sanitized_data_source[key] = ""
    config[CONFIG_KEY_DATA_SOURCE] = sanitized_data_source

    config[CONFIG_KEY_MODE] = MODE_NULL
    config[CONFIG_KEY_PLAYLIST_SELECTION] = []
    config[CONFIG_KEY_PLAYLIST_LIBRARY] = {"Default": []}
    config[CONFIG_KEY_PLAYLIST_ACTIVE] = "Default"
    config[CONFIG_KEY_MONITOR_PLAYLISTS] = {
        key: [] for key in sanitized_data_source.keys()
    }
    config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = {
        key: "Default" for key in sanitized_data_source.keys()
    }

    save_error = None
    try:
        config_util.save(config)
    except OSError as e:
        save_error = str(e)
        logger.warning(f"[Privacy] Could not update config during purge: {e}")

    removed_cache_files = 0
    if os.path.isdir(THUMB_CACHE_DIR):
        for root, _dirs, files in os.walk(THUMB_CACHE_DIR):
            for filename in files:
                file_path = os.path.join(root, filename)
                try:
                    os.remove(file_path)
                    removed_cache_files += 1
                except OSError:
                    pass

    logger.info(
        f"[Privacy] Cleared local app state and removed {removed_cache_files} cached preview files."
    )
    return {
        "ok": save_error is None,
        "error": save_error,
        "config_path": CONFIG_PATH,
        "cache_dir": THUMB_CACHE_DIR,
        "removed_cache_files": removed_cache_files,
    }


def get_gpu_usage_snapshot():
    """
    Return lightweight GPU usage data (best effort).
    Output list item example:
    {
      "vendor": "nvidia",
      "name": "NVIDIA GeForce RTX ...",
      "usage_percent": 31,
      "memory_used_mb": 1200,
      "memory_total_mb": 8192,
      "source": "nvidia-smi"
    }
    """
    snapshot = []

    if shutil.which("nvidia-smi") is not None:
        nvidia_output = _run_text_cmd(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=2,
        )
        if nvidia_output:
            for line in nvidia_output.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 4:
                    continue
                snapshot.append({
                    "vendor": "nvidia",
                    "name": parts[0] or "NVIDIA GPU",
                    "usage_percent": _safe_int(parts[1]),
                    "memory_used_mb": _safe_int(parts[2]),
                    "memory_total_mb": _safe_int(parts[3]),
                    "source": "nvidia-smi",
                })

    has_nvidia_entry = any(item.get("vendor") == "nvidia" for item in snapshot)
    for card_path in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        device_dir = os.path.join(card_path, "device")
        vendor_hex = _read_text_file(os.path.join(device_dir, "vendor")).lower()
        vendor = GPU_VENDOR_MAP.get(vendor_hex, "unknown")
        if vendor == "nvidia" and has_nvidia_entry:
            continue

        usage_percent = None
        for usage_path in (
            os.path.join(device_dir, "gpu_busy_percent"),
            os.path.join(device_dir, "busy_percent"),
        ):
            usage_percent = _safe_int(_read_text_file(usage_path))
            if usage_percent is not None:
                break

        if usage_percent is None:
            continue

        snapshot.append({
            "vendor": vendor,
            "name": os.path.basename(card_path),
            "usage_percent": usage_percent,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "source": "sysfs",
        })

    return snapshot


def setup_autostart(autostart):
    if sys.platform == "win32":
        return
    if is_flatpak():
        """
        Use portal to autostart for Flatpak
        Documentation:
        https://libportal.org/method.Portal.request_background.html
        https://libportal.org/method.Portal.request_background_finish.html 
        """

        gi.require_version("Xdp", "1.0")
        from gi.repository import Xdp
        xdp = Xdp.Portal.new()

        # Request Autostart
        xdp.request_background(
            None,  # parent
            "Autostart Wall Blazer in background",  # reason
            ['wallblazer', '-b'],  # commandline
            Xdp.BackgroundFlags.AUTOSTART if autostart else Xdp.BackgroundFlags.NONE,  # flags
            None,  # cancellable
            lambda portal, result, user_data: logger.debug(
                f"[Utils] autostart={autostart}, request_background sucess={portal.request_background_finish(result)}"),  # callback
            None,  # user_data
        )
        
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    logger.debug(
        f"[Utils] autostart={autostart}, path={AUTOSTART_DESKTOP_PATH}")
    if autostart:
        with open(AUTOSTART_DESKTOP_PATH, mode='w') as f:
            if is_flatpak():
                # Write files to the sandbox as well, for the following reasons:
                # (1) So that we know if autostart is enabled by looking the file in sandbox
                # (2) Acts as a fallback in case the portal doesn't work
                f.write(AUTOSTART_DESKTOP_CONTENT_FLATPAK)
            else:
                f.write(AUTOSTART_DESKTOP_CONTENT)
    else:
        if os.path.isfile(AUTOSTART_DESKTOP_PATH):
            os.remove(AUTOSTART_DESKTOP_PATH)


def get_video_paths():
    file_list = []
    if not os.path.isdir(VIDEO_WALLPAPER_DIR):
        return file_list

    def _has_video_stream(filepath):
        if shutil.which("ffprobe") is None:
            return False
        try:
            ret = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    filepath,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
            return ret.returncode == 0 and "video" in (ret.stdout or "").lower()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    for filename in os.listdir(VIDEO_WALLPAPER_DIR):
        filepath = os.path.join(VIDEO_WALLPAPER_DIR, filename)
        if not os.path.isfile(filepath):
            continue
        file = Gio.file_new_for_path(filepath)
        try:
            info = file.query_info('standard::content-type',
                                   Gio.FileQueryInfoFlags.NONE, None)
            mime_type = (info.get_content_type() or "").lower()
            ext = pathlib.Path(filepath).suffix.lower()
            is_video_mime = ("video" in mime_type) or (mime_type == "application/ogg")
            if is_video_mime or ext in VIDEO_FILE_EXTENSIONS or _has_video_stream(filepath):
                file_list.append(filepath)
        except Exception:
            if (
                pathlib.Path(filepath).suffix.lower() in VIDEO_FILE_EXTENSIONS
                or _has_video_stream(filepath)
            ):
                file_list.append(filepath)
    return sorted(file_list)


def apply_gtk_theme(theme: str):
    """
    Apply GTK dark/light/system theme to the current process.
    theme: 'dark' | 'light' | 'system'
    """
    try:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return
        if theme == "dark":
            settings.props.gtk_application_prefer_dark_theme = True
        else:
            settings.props.gtk_application_prefer_dark_theme = False
    except Exception as e:
        logger.warning(f"[Theme] Could not apply theme '{theme}': {e}")


"""
GNOME extension utils
"""


def gnome_extension_is_enabled(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    info: dict = gnome_ext.GetExtensionInfo(extension_name)
    return info["state"] == 1  # ENABLE = 1


def gnome_extension_set_enable(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    success: bool = gnome_ext.EnableExtension(extension_name)
    return success


def gnome_extension_set_disable(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    success: bool = gnome_ext.DisableExtension(extension_name)
    return success


def gnome_extension_is_installed(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    installed: dict = gnome_ext.ListExtensions()
    return extension_name in installed.keys()


def gnome_desktop_icon_workaround():
    """
    Workaround for GNOME desktop icon extensions not displaying the icons on top of Wall Blazer.
    Call this right after the wallpaper is shown.
    """
    if not is_gnome():
        return
    extension_list = ["ding@rastersoft.com",
                      "desktopicons-neo@darkdemon", 
                      "gtk4-ding@smedius.gitlab.com",
                      "zorin-desktop-icons@zorinos.com"
                      ]
    for ext in extension_list:
        # Check if installed and enabled
        if gnome_extension_is_installed(ext) and gnome_extension_is_enabled(ext):
            # Reload the extension
            logger.info(f"[Utils] Apply workaround for {ext}")
            gnome_extension_set_disable(ext)
            gnome_extension_set_enable(ext)


"""
Handlers
"""


class ActiveHandler:
    """
    Handler for monitoring screen lock
    GNOME:
    https://gitlab.gnome.org/GNOME/gnome-shell/-/blob/main/data/dbus-interfaces/org.gnome.ScreenSaver.xml
    Cinamon:
    https://github.com/linuxmint/cinnamon-screensaver/blob/master/libcscreensaver/org.cinnamon.ScreenSaver.xml
    Freedesktop:
    https://github.com/KDE/kscreenlocker/blob/master/dbus/org.freedesktop.ScreenSaver.xml
    """

    def __init__(self, on_active_changed: callable):
        if sys.platform == "win32" or pydbus is None:
            return
        
        self.session_bus = pydbus.SessionBus()
        self.proxies = []
        self.signal_subscriptions = []
        
        screensaver_list = ["org.gnome.ScreenSaver",
                            "org.cinnamon.ScreenSaver",
                            "org.freedesktop.ScreenSaver"]
        for s in screensaver_list:
            try:
                proxy = self.session_bus.get(s)
                # Store proxy reference to prevent garbage collection
                self.proxies.append(proxy)
                subscription = proxy.ActiveChanged.connect(on_active_changed)
                self.signal_subscriptions.append((proxy, subscription))
            except GLib.Error:
                pass

    def cleanup(self):
        """Cleanup signal subscriptions"""
        for proxy, subscription in self.signal_subscriptions:
            try:
                # Note: pydbus doesn't have a direct disconnect method
                # The connection will be cleaned up when the proxy is garbage collected
                pass
            except Exception as e:
                logger.warning(f"[ActiveHandler] Error during cleanup: {e}")
        self.signal_subscriptions.clear()
        self.proxies.clear()


class EndSessionHandler:
    """
    Handler for monitoring end session
    References:
    https://github.com/backloop/gendsession

    PrepareForShutdown() signal from logind is not handled
    https://gitlab.gnome.org/GNOME/gnome-shell/-/issues/787
    """

    def __init__(self, on_end_session: callable):
        self.on_end_session = on_end_session
        if sys.platform == "win32" or pydbus is None:
            return

        if is_gnome():
            session_bus = pydbus.SessionBus()
            proxy = session_bus.get("org.gnome.SessionManager")
            client_id = proxy.RegisterClient("", "")
            self.session_client = session_bus.get(
                "org.gnome.SessionManager", client_id)
            self.session_client.QueryEndSession.connect(
                self.__query_end_session_handler_gnome)
            self.session_client.EndSession.connect(
                self.__end_session_handler_gnome)
        else:
            system_bus = pydbus.SystemBus()
            proxy = system_bus.get(".login1")
            proxy.PrepareForShutdown.connect(self.__end_session_handler)

    def __end_session_response_gnome(self, ok=True):
        if ok:
            self.session_client.EndSessionResponse(True, "")
        else:
            self.session_client.EndSessionResponse(False, "Not ready")

    def __query_end_session_handler_gnome(self, flags):
        # Ignore flags, always agree on the QueryEndSesion
        self.__end_session_response_gnome(True)

    def __end_session_handler_gnome(self, flags):
        logger.debug("[EndSessionHandler] called")
        self.on_end_session()
        self.__end_session_response_gnome(True)

    def __end_session_handler(self, *_):
        logger.debug("[EndSessionHandler] called")
        self.on_end_session()


class WindowHandler:
    """
    Handler for monitoring window events (maximized and fullscreen mode) for X11
    """

    def __init__(self, on_window_state_changed: callable):
        self.on_window_state_changed = on_window_state_changed
        self.screen = Wnck.Screen.get_default()
        self.screen.force_update()
        
        # Store signal handler IDs for cleanup
        self.signal_handlers = []
        self.window_signal_handlers = {}
        
        # Connect screen signals and store handler IDs
        handler_id = self.screen.connect("window-opened", self.window_opened, None)
        self.signal_handlers.append((self.screen, handler_id))
        
        handler_id = self.screen.connect("window-closed", self.eval, None)
        self.signal_handlers.append((self.screen, handler_id))
        
        handler_id = self.screen.connect("active-workspace-changed", self.eval, None)
        self.signal_handlers.append((self.screen, handler_id))
        
        # Connect to existing windows
        for window in self.screen.get_windows():
            self._connect_window(window)

        self.prev_state = None
        # Initial check
        self.eval()

    def _connect_window(self, window):
        """Connect to a window and store the handler ID"""
        if window not in self.window_signal_handlers:
            handler_id = window.connect("state-changed", self.eval, None)
            self.window_signal_handlers[window] = handler_id

    def window_opened(self, screen, window, _):
        self._connect_window(window)

    def eval(self, *args):
        # TODO: #28 (Wallpaper stops animating on other monitor when app maximized on other)
        is_changed = False

        is_any_maximized, is_any_fullscreen = False, False
        for window in self.screen.get_windows():
            base_state = not Wnck.Window.is_minimized(window) and \
                Wnck.Window.is_on_workspace(
                    window, self.screen.get_active_workspace())
            window_name, is_maximized, is_fullscreen = window.get_name(), \
                Wnck.Window.is_maximized(window) and base_state, \
                Wnck.Window.is_fullscreen(window) and base_state
            if is_maximized is True:
                is_any_maximized = True
            if is_fullscreen is True:
                is_any_fullscreen = True

        cur_state = {"is_any_maximized": is_any_maximized,
                     "is_any_fullscreen": is_any_fullscreen}
        if self.prev_state is None or self.prev_state != cur_state:
            is_changed = True
            self.prev_state = cur_state

        if is_changed:
            self.on_window_state_changed(
                {"is_any_maximized": is_any_maximized, "is_any_fullscreen": is_any_fullscreen})
            logger.debug(f"[WindowHandler] {cur_state}")

    def cleanup(self):
        """Cleanup all signal handlers to prevent memory leaks"""
        # Disconnect screen signals
        for obj, handler_id in self.signal_handlers:
            try:
                obj.disconnect(handler_id)
            except Exception as e:
                logger.warning(f"[WindowHandler] Error disconnecting screen signal: {e}")
        self.signal_handlers.clear()
        
        # Disconnect window signals
        for window, handler_id in self.window_signal_handlers.items():
            try:
                window.disconnect(handler_id)
            except Exception as e:
                logger.warning(f"[WindowHandler] Error disconnecting window signal: {e}")
        self.window_signal_handlers.clear()


# class WindowHandlerGnome:
#     """
#     Handler for monitoring window events for Gnome only
#     TODO: This is broken due to a change in GNOME =(
#     https://gitlab.gnome.org/GNOME/gnome-shell/-/commit/7298ee23e91b756c7009b4d7687dfd8673856f8b

#     TLDR, there is no way to monitor window events in Wayland, unless we use an Shell extension.
#     To bypass, execute the below line in looking glass (Alt+F2 `lg`)
#     `global.context.unsafe_mode = true`
#     """

#     def __init__(self, on_window_state_changed: callable):
#         self.on_window_state_changed = on_window_state_changed
#         self.gnome_shell = pydbus.SessionBus().get("org.gnome.Shell")
#         self.prev_state = None
#         display = Gdk.Display.get_default()
#         self.num_monitor = display.get_n_monitors()
#         GLib.timeout_add(500, self.eval)

#     def eval(self):
#         is_changed = False

#         ret1, workspace = self.gnome_shell.Eval("""
#                         global.workspace_manager.get_active_workspace_index()
#                         """)
#         ret2 = False
#         maximized = []
#         for monitor in range(self.num_monitor):
#             ret2, temp = self.gnome_shell.Eval(f"""
#                             var window_list = global.get_window_actors().find(window =>
#                                 window.meta_window.maximized_horizontally &
#                                 window.meta_window.maximized_vertically &
#                                 !window.meta_window.minimized &
#                                 window.meta_window.get_workspace().workspace_index == {workspace} &
#                                 window.meta_window.get_monitor() == {monitor}
#                             );
#                             window_list
#                             """)
#             maximized.append(temp != "")
#         # Every monitors have a maximized window?
#         maximized = all(maximized)

#         ret3 = False
#         fullscreen = []
#         for monitor in range(self.num_monitor):
#             ret3, temp = self.gnome_shell.Eval(f"""
#                             var window_list = global.get_window_actors().find(window =>
#                     window.meta_window.is_fullscreen() &
#                     !window.meta_window.minimized &
#                     window.meta_window.get_workspace().workspace_index == {workspace} &
#                     window.meta_window.get_monitor() == {monitor}
#                 );
#                 window_list
#                 """)
#             fullscreen.append(temp != "")
#         # Every monitors have a fullscreen window?
#         fullscreen = all(fullscreen)

#         if not all([ret1, ret2, ret3]):
#             logging.error(
#                 "[WindowHandlerGnome] Cannot communicate with Gnome Shell!")

#         cur_state = {'is_any_maximized': maximized,
#                      'is_any_fullscreen': fullscreen}
#         if self.prev_state is None or self.prev_state != cur_state:
#             is_changed = True
#             self.prev_state = cur_state

#         if is_changed:
#             self.on_window_state_changed(
#                 {"is_any_maximized": maximized, "is_any_fullscreen": fullscreen})
#             logger.debug(f"[WindowHandlerGnome] {cur_state}")
#         return True


class ConfigUtil:
    def generate_template(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self.save(CONFIG_TEMPLATE)

    @staticmethod
    def _check(config: dict):
        """Check if the config is valid"""
        is_all_keys_match = all(key in config for key in CONFIG_TEMPLATE)
        is_version_match = config.get("version") == CONFIG_VERSION
        return is_all_keys_match and is_version_match

    def _invalid(self):
        logger.debug(f"[Config] Invalid. A new config will be generated.")
        self.generate_template()
        return CONFIG_TEMPLATE

    @staticmethod
    def _normalize_playlist_items(items):
        if not isinstance(items, list):
            return []
        seen = set()
        normalized = []
        for item in items:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _unique_playlist_name(base_name, existing_playlists: dict):
        base = str(base_name).strip() or "Playlist"
        if base not in existing_playlists:
            return base
        i = 2
        while f"{base} {i}" in existing_playlists:
            i += 1
        return f"{base} {i}"

    def _normalize_playlist_config(self, config: dict):
        changed = False
        library = config.get(CONFIG_KEY_PLAYLIST_LIBRARY)
        normalized_library = {}
        if isinstance(library, dict):
            for raw_name, raw_items in library.items():
                name = str(raw_name).strip()
                if not name:
                    continue
                normalized_library[name] = self._normalize_playlist_items(raw_items)

        if not normalized_library:
            normalized_library = {
                "Default": self._normalize_playlist_items(
                    config.get(CONFIG_KEY_PLAYLIST_SELECTION, [])
                )
            }
            changed = True

        active_name = config.get(CONFIG_KEY_PLAYLIST_ACTIVE, "Default")
        if not isinstance(active_name, str) or active_name not in normalized_library:
            active_name = next(iter(normalized_library.keys()))
            changed = True

        monitor_names = []
        data_source = config.get(CONFIG_KEY_DATA_SOURCE, {})
        if isinstance(data_source, dict):
            for monitor_name in data_source.keys():
                if isinstance(monitor_name, str):
                    monitor_names.append(monitor_name)
        if "Default" not in monitor_names:
            monitor_names.append("Default")

        monitor_playlists = config.get(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        if not isinstance(monitor_playlists, dict):
            monitor_playlists = {}
            changed = True

        assignments = config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if not isinstance(assignments, dict):
            assignments = {}
            changed = True

        normalized_assignments = {}
        for monitor_name in monitor_names:
            assigned_playlist = assignments.get(monitor_name)
            if not isinstance(assigned_playlist, str) or assigned_playlist not in normalized_library:
                legacy_items = self._normalize_playlist_items(monitor_playlists.get(monitor_name, []))
                assigned_playlist = None
                if legacy_items:
                    for playlist_name, playlist_items in normalized_library.items():
                        if playlist_items == legacy_items:
                            assigned_playlist = playlist_name
                            break
                    if assigned_playlist is None:
                        preferred_name = "Default" if monitor_name == "Default" else f"{monitor_name} Playlist"
                        if preferred_name in normalized_library and normalized_library.get(preferred_name) != legacy_items:
                            preferred_name = self._unique_playlist_name(preferred_name, normalized_library)
                        normalized_library[preferred_name] = legacy_items
                        assigned_playlist = preferred_name
                        changed = True
                if assigned_playlist is None:
                    assigned_playlist = active_name
                changed = True
            normalized_assignments[monitor_name] = assigned_playlist

        for monitor_name, assigned_playlist in assignments.items():
            if monitor_name in normalized_assignments:
                continue
            if not isinstance(monitor_name, str):
                continue
            if isinstance(assigned_playlist, str) and assigned_playlist in normalized_library:
                normalized_assignments[monitor_name] = assigned_playlist

        derived_monitor_playlists = {}
        for monitor_name, playlist_name in normalized_assignments.items():
            derived_monitor_playlists[monitor_name] = list(
                normalized_library.get(playlist_name, [])
            )

        active_items = normalized_library.get(active_name, [])
        if config.get(CONFIG_KEY_PLAYLIST_SELECTION) != active_items:
            config[CONFIG_KEY_PLAYLIST_SELECTION] = list(active_items)
            changed = True

        if config.get(CONFIG_KEY_PLAYLIST_LIBRARY) != normalized_library:
            config[CONFIG_KEY_PLAYLIST_LIBRARY] = normalized_library
            changed = True

        if config.get(CONFIG_KEY_PLAYLIST_ACTIVE) != active_name:
            config[CONFIG_KEY_PLAYLIST_ACTIVE] = active_name
            changed = True

        if config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS) != normalized_assignments:
            config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = normalized_assignments
            changed = True

        if config.get(CONFIG_KEY_MONITOR_PLAYLISTS) != derived_monitor_playlists:
            config[CONFIG_KEY_MONITOR_PLAYLISTS] = derived_monitor_playlists
            changed = True

        return changed
        
    def _migrateV3To4(self, config: dict):
        logger.debug(f"[Config] Migration from version 3 to 4.")
        curr_data_source = config['data_source']
        config['data_source'] = CONFIG_TEMPLATE[CONFIG_KEY_DATA_SOURCE]
        config['data_source']['Default'] = curr_data_source
        config['is_pause_when_maximized'] = config["is_detect_maximized"]
        del config["is_detect_maximized"]
        config['is_mute_when_maximized'] = CONFIG_TEMPLATE[CONFIG_KEY_MUTE_WHEN_MAXIMIZED]
        config['version'] = 4
        # save config file
        self.save(config)
        
    def _checkMissingMonitors(self, old_config: dict, template: dict):
        # Extract the monitors from both configurations
        old_monitors = old_config.get("data_source", {}).keys()
        template_monitors = template.get("data_source", {}).keys()
        # Find monitors in the template that are not in the old configuration
        missing_monitors = set(template_monitors) - set(old_monitors)
        if len(missing_monitors) > 0:
            logger.warning(f"[Config] There are missing {len(missing_monitors)} monitors in config. Creating default one")
            self._createMissingMonitors(missing_monitors, old_config)
    
    def _createMissingMonitors(self, keys: set, config: dict):
        # we will set to Default new monitor sources
        monitor_playlists = config.setdefault(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        monitor_assignments = config.setdefault(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        playlist_library = config.setdefault(CONFIG_KEY_PLAYLIST_LIBRARY, {"Default": []})
        active_playlist = config.get(CONFIG_KEY_PLAYLIST_ACTIVE, "Default")
        if active_playlist not in playlist_library:
            active_playlist = next(iter(playlist_library.keys()))
        for key in keys:
            config['data_source'][key] = config['data_source']['Default']
            if key not in monitor_assignments:
                monitor_assignments[key] = active_playlist
            assigned = monitor_assignments.get(key, active_playlist)
            monitor_playlists[key] = list(playlist_library.get(assigned, []))
        self.save(config)
        
    def _checkDefaultSource(self, config: dict):
        # Check if the 'Default' source is empty
        default_source = config['data_source'].get('Default', '')
        mode = config.get('mode')
        if mode == MODE_VIDEO and not os.path.isfile(default_source):
            logger.warning("[Config] Default source is empty or not a valid file. Setting to the first on available.")
            
            # Get all values from the 'data_source' dictionary
            values = list(config['data_source'].values())
            # If there are no values in 'data_source', return early
            if not values:
                return
            
            # Set the 'Default' source to the first value available
            for value in values:
                if len(value) > 0 and os.path.isfile(value):
                    config['data_source']['Default'] = value
                    self.save(config)
                    break
                    
    def load(self):
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                json_str = f.read()
                try:
                    config = json.loads(json_str)
                    # migration: versions <= 3 need data_source restructure
                    if config.get("version", 0) <= 3 and CONFIG_VERSION >= 4:
                        self._migrateV3To4(config)
                    # migration v4 -> v5: add playlist + theme keys
                    if config.get("version", 0) == 4 and CONFIG_VERSION >= 5:
                        config[CONFIG_KEY_PLAYLIST] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST]
                        config[CONFIG_KEY_PLAYLIST_INTERVAL] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST_INTERVAL]
                        config[CONFIG_KEY_PLAYLIST_SHUFFLE] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST_SHUFFLE]
                        config[CONFIG_KEY_THEME] = CONFIG_TEMPLATE[CONFIG_KEY_THEME]
                        config["version"] = 5
                        self.save(config)
                    # migration v5 -> v6: add explicit playlist selection
                    if config.get("version", 0) == 5 and CONFIG_VERSION >= 6:
                        config[CONFIG_KEY_PLAYLIST_SELECTION] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST_SELECTION]
                        config["version"] = 6
                        self.save(config)
                    # migration v6 -> v7: add named playlist library + active playlist key
                    if config.get("version", 0) == 6 and CONFIG_VERSION >= 7:
                        config[CONFIG_KEY_PLAYLIST_LIBRARY] = {
                            "Default": self._normalize_playlist_items(
                                config.get(CONFIG_KEY_PLAYLIST_SELECTION, [])
                            )
                        }
                        config[CONFIG_KEY_PLAYLIST_ACTIVE] = "Default"
                        config["version"] = 7
                        self.save(config)
                    # migration v7 → v8: add per-monitor playlists
                    if config.get("version", 0) == 7 and CONFIG_VERSION >= 8:
                        # Build initial monitor_playlists from existing playlist_selection
                        existing_selection = self._normalize_playlist_items(
                            config.get(CONFIG_KEY_PLAYLIST_SELECTION, [])
                        )
                        monitor_playlists = {}
                        data_src = config.get(CONFIG_KEY_DATA_SOURCE, {})
                        for monitor_name in data_src:
                            # Give every monitor the existing playlist selection as a starting point
                            monitor_playlists[monitor_name] = list(existing_selection)
                        config[CONFIG_KEY_MONITOR_PLAYLISTS] = monitor_playlists
                        config["version"] = 8
                        self.save(config)
                    # migration v8 -> v9: add per-monitor playlist assignments
                    if config.get("version", 0) == 8 and CONFIG_VERSION >= 9:
                        config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = {}
                        config["version"] = 9
                        self._normalize_playlist_config(config)
                        self.save(config)
                    if self._normalize_playlist_config(config):
                        self.save(config)
                    self._checkDefaultSource(config)
                    self._checkMissingMonitors(config, CONFIG_TEMPLATE)
                    if self._check(config):
                        summary = _redacted_config_summary(config)
                        logger.debug(f"[Config] Loaded {CONFIG_PATH} summary={summary}")
                        return config
                except json.decoder.JSONDecodeError:
                    logger.debug(f"[Config] JSONDecodeError")
        return self._invalid()

    def save(self, config):
        old_config = None
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                json_str = f.read()
                try:
                    old_config = json.loads(json_str)
                    if not self._check(old_config):
                        old_config = None
                except json.decoder.JSONDecodeError:
                    old_config = None
        # Skip if the config is identical
        if old_config == config:
            return
        with open(CONFIG_PATH, "w") as f:
            json_str = json.dumps(config, indent=3)
            print(json_str, file=f)
            summary = _redacted_config_summary(config)
            logger.debug(f"[Config] Saved {CONFIG_PATH} summary={summary}")
