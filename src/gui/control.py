import sys
import subprocess
import logging
import threading
import os
import shutil
import mimetypes
import requests
import multiprocessing as mp
import setproctitle
from datetime import datetime

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, GLib, Gdk, Pango

import yt_dlp

try:
    sys.path.insert(1, os.path.join(sys.path[0], "."))
    sys.path.insert(1, os.path.join(sys.path[0], ".."))
    from commons import *
    from ipc import get_service
    from monitor import *
    from gui.gui_utils import debounce, request_thumbnail_pixbuf
    from utils import (
        ConfigUtil, setup_autostart, is_gnome, is_wayland,
        get_video_paths, apply_gtk_theme, get_vlc_hwdec_profile,
        get_gpu_usage_snapshot, normalize_video_file, video_needs_normalization,
        open_path_default,
    )
except (ModuleNotFoundError, ImportError):
    from wallblazer.monitor import *
    from wallblazer.commons import *
    from wallblazer.ipc import get_service
    from wallblazer.gui.gui_utils import debounce, request_thumbnail_pixbuf
    from wallblazer.utils import (
        ConfigUtil, setup_autostart, is_gnome, is_wayland,
        get_video_paths, apply_gtk_theme, get_vlc_hwdec_profile,
        get_gpu_usage_snapshot, normalize_video_file, video_needs_normalization,
        open_path_default,
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(LOGGER_NAME)

APP_ID = f"{PROJECT}.gui"
APP_TITLE = "Wall Blazer"
APP_UI_RESOURCE_PATH = "/io/wallblazer/WallBlazer/control.ui"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _is_truthy_env(var_name):
    value = str(os.environ.get(var_name, "")).strip().lower()
    return value in _TRUTHY_ENV_VALUES


def _iter_ui_fallback_candidates():
    paths = []
    seen = set()

    def _push(path):
        if not isinstance(path, str):
            return
        value = path.strip()
        if not value:
            return
        norm = os.path.normpath(os.path.abspath(value))
        if norm in seen:
            return
        seen.add(norm)
        paths.append(norm)

    pkgdatadir = os.environ.get("WALLBLAZER_PKGDATADIR", "")
    if pkgdatadir:
        _push(os.path.join(pkgdatadir, "control.ui"))
        _push(os.path.join(pkgdatadir, "assets", "control.ui"))

    _push(os.environ.get("WALLBLAZER_UI_PATH", ""))

    module_dir = os.path.abspath(os.path.dirname(__file__))
    exe_dir = os.path.abspath(os.path.dirname(sys.executable))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        _push(os.path.join(meipass, "assets", "control.ui"))
        _push(os.path.join(meipass, "src", "assets", "control.ui"))

    for root in (
        os.getcwd(),
        module_dir,
        os.path.abspath(os.path.join(module_dir, "..")),
        exe_dir,
        os.path.join(exe_dir, "_internal"),
    ):
        _push(os.path.join(root, "assets", "control.ui"))
        _push(os.path.join(root, "src", "assets", "control.ui"))
    return paths

# ─────────────────────────────────────────────────
#  Application CSS
# ─────────────────────────────────────────────────
NEON_CSS_DARK = """
window.wb-root {
  background-color: #101214;
  color: #edf2ef;
}
headerbar.wb-header .wb-tab-switcher button {
  background-color: #1a1f24;
  color: #c9d3d0;
  border-radius: 7px;
  border: 1px solid rgba(158, 171, 166, 0.24);
  margin: 0 3px;
  padding: 4px 10px;
  transition: background-color 150ms ease, border-color 150ms ease, color 100ms ease;
}
.wb-tab-switcher button:checked,
.wb-tab-switcher button:hover {
  background-color: #21433d;
  color: #ffffff;
  border: 1px solid rgba(87, 199, 171, 0.66);
}
.wb-panel {
  background-color: #161a1e;
  border-radius: 8px;
  border: 1px solid rgba(158, 171, 166, 0.22);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
  padding: 10px;
}
.wb-chip {
  background-color: #20262b;
  color: #e4ebe8;
  border-radius: 7px;
  border: 1px solid rgba(87, 199, 171, 0.28);
  padding: 0 10px;
  transition: background-color 150ms ease, border-color 150ms ease;
}
button {
  background-color: #20262b;
  color: #edf2ef;
  border: 1px solid rgba(158, 171, 166, 0.28);
  border-radius: 7px;
  transition: background-color 150ms ease, box-shadow 150ms ease;
}
button:hover {
  background-color: #283038;
  box-shadow: 0 0 0 1px rgba(87, 199, 171, 0.3);
}
button:active {
  background-color: #18201f;
}
button.suggested-action {
  background-color: #2f8f7b;
  border: 1px solid rgba(118, 232, 203, 0.82);
  color: #ffffff;
}
entry, spinbutton, combobox, combobox box {
  background-color: #111518;
  color: #edf2ef;
  border: 1px solid rgba(158, 171, 166, 0.34);
  border-radius: 7px;
}
.wb-icon-grid {
  background-color: #121619;
  border: 1px solid rgba(158, 171, 166, 0.18);
  border-radius: 8px;
}
flowboxchild {
  border-radius: 8px;
  border: 1px solid transparent;
  background-color: rgba(32, 38, 43, 0.68);
  transition: border-color 120ms ease, background-color 120ms ease;
}
flowboxchild:hover {
  border-color: rgba(87, 199, 171, 0.42);
  background-color: rgba(43, 53, 56, 0.88);
}
flowboxchild:selected {
  background-color: rgba(47, 143, 123, 0.72);
  border-color: rgba(118, 232, 203, 0.86);
}
iconview.view:selected,
iconview.view cell:selected,
iconview.view .cell:selected,
.wb-icon-grid:selected,
.wb-icon-grid cell:selected {
  background-color: rgba(47, 143, 123, 0.72);
  color: #ffffff;
  border-radius: 7px;
}
popover.wb-popover, popover.wb-popover box {
  background-color: #171c20;
  color: #edf2ef;
}
separator {
  background-color: rgba(158, 171, 166, 0.22);
}
treeview, treeview header button {
  background-color: #13171a;
  color: #edf2ef;
}
treeview:selected {
  background-color: rgba(47, 143, 123, 0.78);
}
list, list row {
  background-color: #13171a;
  color: #edf2ef;
}
"""

NEON_CSS_LIGHT = """
window.wb-root {
  background-color: #f4f5f2;
  color: #121615;
}
window.wb-root, window.wb-root * {
  color: #121615;
}
headerbar.wb-header {
  background-color: #ecefeb;
  color: #121615;
  border-bottom: 1px solid rgba(61, 86, 78, 0.22);
}
.wb-tab-switcher button {
  background-color: #ffffff;
  color: #23312e;
  border-radius: 7px;
  border: 1px solid rgba(61, 86, 78, 0.22);
  margin: 0 3px;
  padding: 4px 10px;
  transition: background-color 150ms ease, border-color 150ms ease, color 100ms ease;
}
.wb-tab-switcher button:checked,
.wb-tab-switcher button:hover {
  background-color: #2f8f7b;
  color: #ffffff;
  border: 1px solid rgba(28, 105, 88, 0.9);
}
.wb-panel {
  background-color: #ffffff;
  border-radius: 8px;
  border: 1px solid rgba(61, 86, 78, 0.16);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.85);
  padding: 10px;
}
.wb-chip {
  background-color: #f7f9f6;
  color: #23312e;
  border-radius: 7px;
  border: 1px solid rgba(47, 143, 123, 0.28);
  padding: 0 10px;
  transition: background-color 150ms ease, border-color 150ms ease;
}
button {
  background-color: #ffffff;
  color: #121615;
  border: 1px solid rgba(61, 86, 78, 0.22);
  border-radius: 7px;
  transition: background-color 150ms ease, box-shadow 150ms ease;
}
button:hover {
  background-color: #edf4f1;
  box-shadow: 0 0 0 1px rgba(47, 143, 123, 0.22);
}
button:active {
  background-color: #dce9e5;
}
button.suggested-action {
  background-color: #2f8f7b;
  border: 1px solid rgba(28, 105, 88, 0.95);
  color: #ffffff;
}
entry, spinbutton, combobox, combobox box {
  background-color: #ffffff;
  color: #121615;
  border: 1px solid rgba(61, 86, 78, 0.26);
  border-radius: 7px;
}
.wb-icon-grid {
  background-color: #fafbf8;
  border: 1px solid rgba(61, 86, 78, 0.14);
  border-radius: 8px;
}
flowboxchild {
  border-radius: 8px;
  border: 1px solid transparent;
  background-color: rgba(240, 244, 240, 0.78);
  transition: border-color 120ms ease, background-color 120ms ease;
}
flowboxchild:hover {
  border-color: rgba(47, 143, 123, 0.34);
  background-color: rgba(225, 238, 233, 0.85);
}
flowboxchild:selected {
  background-color: rgba(47, 143, 123, 0.22);
  border-color: rgba(28, 105, 88, 0.72);
}
iconview.view:selected,
iconview.view cell:selected,
iconview.view .cell:selected,
.wb-icon-grid:selected,
.wb-icon-grid cell:selected {
  background-color: rgba(47, 143, 123, 0.22);
  color: #121615;
  border-radius: 7px;
}
popover.wb-popover, popover.wb-popover box {
  background-color: #ffffff;
  color: #121615;
}
separator {
  background-color: rgba(61, 86, 78, 0.18);
}
treeview, treeview header button {
  background-color: #ffffff;
  color: #121615;
}
treeview:selected {
  background-color: rgba(47, 143, 123, 0.22);
}
list, list row {
  background-color: #ffffff;
  color: #121615;
}
list row:selected {
  background-color: rgba(47, 143, 123, 0.22);
}
"""


class VideoGridTile(Gtk.FlowBoxChild):
    # How many preview frames to extract for hover animation
    _PREVIEW_FRAMES = 8
    # Milliseconds between preview frames
    _PREVIEW_INTERVAL_MS = 160
    # Thread pool shared across all tiles for preview frame extraction
    _PREVIEW_EXECUTOR = None

    @classmethod
    def _get_preview_executor(cls):
        from concurrent.futures import ThreadPoolExecutor
        if cls._PREVIEW_EXECUTOR is None:
            cls._PREVIEW_EXECUTOR = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="wb-preview"
            )
        return cls._PREVIEW_EXECUTOR

    def __init__(self, video_path, width=240, height=135):
        super().__init__()
        self.video_path = video_path
        self._thumb_width = width
        self._thumb_height = height
        self._thumb_requested = False
        self._disposed = False
        self._static_pixbuf = None

        # Hover animation state
        self._preview_frames = []   # list of GdkPixbuf
        self._preview_timer_id = None
        self._preview_frame_idx = 0
        self._preview_loaded = False
        self._preview_loading = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(6)
        root.set_margin_bottom(6)
        root.set_margin_start(6)
        root.set_margin_end(6)

        self.thumb_image = Gtk.Image.new_from_icon_name("video-x-generic", Gtk.IconSize.DIALOG)
        self.thumb_image.set_size_request(self._thumb_width, self._thumb_height)
        self.thumb_image.set_hexpand(True)
        self.thumb_image.set_vexpand(True)
        self.thumb_image.set_halign(Gtk.Align.FILL)
        self.thumb_image.set_valign(Gtk.Align.FILL)

        self.name_label = Gtk.Label(label=os.path.basename(video_path))
        self.name_label.set_xalign(0.0)
        self.name_label.set_max_width_chars(36)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)

        root.pack_start(self.thumb_image, True, True, 0)
        root.pack_start(self.name_label, False, False, 0)
        
        self.event_box = Gtk.EventBox()
        self.event_box.add(root)
        self.add(self.event_box)

        # Enable enter/leave hover events on the EventBox
        self.event_box.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.event_box.connect("enter-notify-event", self._on_hover_enter)
        self.event_box.connect("leave-notify-event", self._on_hover_leave)

        self.show_all()

    # ── Static thumbnail ──────────────────────────────
    def _on_thumbnail_ready(self, pixbuf):
        if self._disposed:
            return False
        self._static_pixbuf = pixbuf
        if pixbuf is None:
            self.thumb_image.set_from_icon_name("video-x-generic", Gtk.IconSize.DIALOG)
            return False
        try:
            self.thumb_image.set_from_pixbuf(pixbuf)
        except Exception as e:
            logger.debug(f"[VideoTile] Failed to apply thumbnail for {self.video_path}: {e}")
            self.thumb_image.set_from_icon_name("video-x-generic", Gtk.IconSize.DIALOG)
        return False

    def start(self):
        if self._disposed or self._thumb_requested:
            return False
        self._thumb_requested = True
        request_thumbnail_pixbuf(
            self.video_path,
            width=self._thumb_width,
            height=self._thumb_height,
            on_ready=self._on_thumbnail_ready,
        )
        return True

    def stop(self):
        self._stop_preview_animation()

    # ── Hover animated preview ────────────────────────
    def _on_hover_enter(self, _widget, _event):
        if self._disposed:
            return False
        if self._preview_loaded and self._preview_frames:
            self._start_preview_animation()
        elif not self._preview_loading:
            self._preview_loading = True
            self._get_preview_executor().submit(self._load_preview_frames_bg)
        return False

    def _on_hover_leave(self, _widget, _event):
        if self._disposed:
            return False
        self._stop_preview_animation()
        # Restore static thumbnail
        if not self._disposed:
            try:
                if self._static_pixbuf is not None:
                    self.thumb_image.set_from_pixbuf(self._static_pixbuf)
                else:
                    self.thumb_image.set_from_icon_name("video-x-generic", Gtk.IconSize.DIALOG)
            except Exception:
                pass
        return False

    def _load_preview_frames_bg(self):
        """Extract N evenly-spaced frames from the video in a background thread."""
        if self._disposed:
            return
        frames = []
        try:
            import subprocess as _sp
            # Get duration
            try:
                out = _sp.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", self.video_path],
                    timeout=8, text=True, stderr=_sp.DEVNULL
                ).strip()
                duration = float(out)
            except Exception:
                duration = 10.0

            n = self._PREVIEW_FRAMES
            for i in range(n):
                if self._disposed:
                    break
                # Spread frames across 10%–90% of the video
                seek = duration * (0.1 + 0.8 * i / max(n - 1, 1))
                import tempfile
                import os as _os
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".jpg", delete=False, prefix="wb-prev-"
                )
                tmp.close()
                tmp_path = tmp.name
                ret = _sp.run(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-ss", f"{seek:.3f}", "-i", self.video_path,
                     "-frames:v", "1", "-an",
                     "-vf", f"scale={self._thumb_width}:{self._thumb_height}:force_original_aspect_ratio=decrease,pad={self._thumb_width}:{self._thumb_height}:(ow-iw)/2:(oh-ih)/2",
                     tmp_path],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10
                )
                if ret.returncode == 0 and _os.path.exists(tmp_path) and _os.path.getsize(tmp_path) > 0:
                    try:
                        from gi.repository import GdkPixbuf
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(
                            tmp_path, self._thumb_width, self._thumb_height
                        )
                        frames.append(pb)
                    except Exception:
                        pass
                try:
                    _os.remove(tmp_path)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[VideoTile] Preview frame extraction failed for {self.video_path}: {e}")

        def _on_frames_ready():
            if self._disposed:
                return False
            self._preview_loading = False
            if frames:
                self._preview_frames = frames
                self._preview_loaded = True
                # Start animation immediately if still hovered
                self._preview_frame_idx = 0
                self._start_preview_animation()
            return False

        GLib.idle_add(_on_frames_ready)

    def _start_preview_animation(self):
        if self._preview_timer_id is not None or not self._preview_frames:
            return
        self._preview_frame_idx = 0
        self._preview_timer_id = GLib.timeout_add(
            self._PREVIEW_INTERVAL_MS, self._preview_tick
        )

    def _stop_preview_animation(self):
        if self._preview_timer_id is not None:
            try:
                GLib.source_remove(self._preview_timer_id)
            except Exception:
                pass
            self._preview_timer_id = None

    def _preview_tick(self):
        if self._disposed or not self._preview_frames:
            self._preview_timer_id = None
            return False
        try:
            pb = self._preview_frames[self._preview_frame_idx % len(self._preview_frames)]
            self.thumb_image.set_from_pixbuf(pb)
            self._preview_frame_idx += 1
        except Exception:
            pass
        return True  # keep timer running

    def cleanup(self):
        # Set disposed flag first so any in-flight GLib.idle_add callbacks
        # from _load_preview_frames_bg bail out before touching widgets (Bug 7).
        self._disposed = True
        self._stop_preview_animation()
        self._preview_frames = []
        self._static_pixbuf = None


class ControlPanel(Gtk.Application):
    _THUMBNAIL_WARMUP_MAX = 96
    _THUMBNAIL_WARMUP_INTERVAL_MS = 120

    def __init__(self, version, *args, **kwargs):
        super().__init__(
            *args,
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
            **kwargs,
        )
        setproctitle.setproctitle(mp.current_process().name)

        self.builder = Gtk.Builder()
        self.builder.set_application(self)
        try:
            self.builder.add_from_resource(APP_UI_RESOURCE_PATH)
        except GLib.Error:
            loaded = False
            for ui_path in _iter_ui_fallback_candidates():
                if not os.path.isfile(ui_path):
                    continue
                self.builder.add_from_file(ui_path)
                logger.info(f"[GUI] Loaded fallback UI file: {ui_path}")
                loaded = True
                break
            if not loaded:
                raise

        # Wire signal handlers declared in control.ui
        signals = {
            "on_volume_changed": self.on_volume_changed,
            "on_streaming_activate": self.on_streaming_activate,
            "on_web_page_activate": self.on_web_page_activate,
            "on_blur_radius_changed": self.on_blur_radius_changed,
        }
        self.builder.connect_signals(signals)

        self.version = version
        self.window = None
        self.server = None
        self.icon_view = None
        self.video_paths = []
        self.all_key = "all"
        self._css_provider = Gtk.CssProvider()
        self._icon_view_handler_id = None
        self._icon_view_selection_handler_id = None
        self._icon_view_key_handler_id = None
        self._gpu_profile = None
        self._gpu_status_timer_id = None
        self._video_search_query = ""
        self._file_manager_mode = True
        self._fm_clipboard_mode = None  # "copy" | "cut" | None
        self._fm_clipboard_paths = []
        self._fm_context_items = {}
        self._video_normalizer_thread = None
        self._video_normalizer_pending = False
        self._advanced_widgets = {}
        self._video_profile_push_source_id = None
        self._syncing_advanced_widgets = False

        # Local-video grid playback
        self._video_tiles = {}
        self._video_grid_warmup_source_id = None
        # Per-monitor playlist tab state
        self._playlist_selected_monitor = None   # currently selected monitor name in ListBox
        self._playlist_store = None              # GtkListStore for TreeViewPlaylist
        self._playlist_combo_handler_id = None   # signal ID for playlist combobox
        self._monitor_listbox_handler_id = None  # signal ID for monitor ListBox row-selected (Bug 2)

        self.is_autostart = os.path.isfile(AUTOSTART_DESKTOP_PATH)
        self._connect_server()
        self._load_config()
        self._file_manager_mode = bool(self.config.get("file_manager_mode", True))

        # Initialize monitors
        self.monitors = Monitors()
        video_paths = self.config[CONFIG_KEY_DATA_SOURCE]
        for monitor_name in self.monitors.get_monitors():
            src = video_paths.get(monitor_name, video_paths.get("Default", ""))
            self.monitors.get_monitor(monitor_name).set_wallpaper(src)
        if self._sync_monitor_playlists_from_library():
            self._save_config()

        self._setup_context_menu()

    # ──────────────────────────────────────────────────
    #  Server / Config helpers
    # ──────────────────────────────────────────────────
    def _connect_server(self):
        try:
            self.server = get_service(DBUS_NAME_SERVER)
        except Exception:
            logger.error("[GUI] Couldn't connect to server")

    def _load_config(self):
        self.config = ConfigUtil().load()
        # Ensure playlist keys exist for runtime operations
        if CONFIG_KEY_MONITOR_PLAYLISTS not in self.config:
            self.config[CONFIG_KEY_MONITOR_PLAYLISTS] = {}
        if CONFIG_KEY_PLAYLIST_LIBRARY not in self.config:
            self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = {"Default": []}
        if CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS not in self.config:
            self.config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = {}
        if CONFIG_KEY_PLAYLIST_ACTIVE not in self.config:
            self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = "Default"
        if CONFIG_KEY_REVERSE_SINGLE not in self.config:
            self.config[CONFIG_KEY_REVERSE_SINGLE] = False
        if CONFIG_KEY_REVERSE_PLAYLIST not in self.config:
            self.config[CONFIG_KEY_REVERSE_PLAYLIST] = False
        if CONFIG_KEY_REVERSE_PLAYLIST_ITEMS not in self.config:
            self.config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = {}
        if CONFIG_KEY_VIDEO_FIT_MODE not in self.config:
            self.config[CONFIG_KEY_VIDEO_FIT_MODE] = CONFIG_TEMPLATE[CONFIG_KEY_VIDEO_FIT_MODE]
        if CONFIG_KEY_PLAYBACK_SPEED_SINGLE not in self.config:
            self.config[CONFIG_KEY_PLAYBACK_SPEED_SINGLE] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_SINGLE]
        if CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST not in self.config:
            self.config[CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST]
        if CONFIG_KEY_PLAYBACK_SPEED_REVERSE not in self.config:
            self.config[CONFIG_KEY_PLAYBACK_SPEED_REVERSE] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_REVERSE]
        if CONFIG_KEY_VIDEO_ADJUSTMENTS not in self.config:
            self.config[CONFIG_KEY_VIDEO_ADJUSTMENTS] = dict(CONFIG_TEMPLATE[CONFIG_KEY_VIDEO_ADJUSTMENTS])
        self.config.setdefault("file_manager_mode", True)

    def _save_config(self):
        ConfigUtil().save(self.config)

    @debounce(1)
    def _save_config_delay(self):
        self._save_config()

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
    def _unique_playlist_name(base_name, existing_names):
        base = str(base_name).strip() or "Playlist"
        if base not in existing_names:
            return base
        i = 2
        while f"{base} {i}" in existing_names:
            i += 1
        return f"{base} {i}"

    def _playlist_names(self):
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict):
            return []
        names = [
            str(name).strip()
            for name in library.keys()
            if isinstance(name, str) and str(name).strip()
        ]
        if "Default" in names:
            names.remove("Default")
            names.insert(0, "Default")
        return names

    def _reverse_items_for_playlist(self, playlist_name):
        reverse_items = self.config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if not isinstance(reverse_items, dict):
            return set()
        items = reverse_items.get(playlist_name, [])
        if not isinstance(items, list):
            return set()
        return set(self._normalize_playlist_items(items))

    def _prune_reverse_items_for_playlist(self, playlist_name, playlist_items):
        reverse_items = self.config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if not isinstance(reverse_items, dict):
            reverse_items = {}
        items = reverse_items.get(playlist_name, [])
        if not isinstance(items, list):
            items = []
        playlist_set = set(self._normalize_playlist_items(playlist_items))
        filtered = [item for item in self._normalize_playlist_items(items) if item in playlist_set]
        if filtered == items:
            return False
        if filtered:
            reverse_items[playlist_name] = filtered
        else:
            reverse_items.pop(playlist_name, None)
        self.config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = reverse_items
        return True

    def _normalize_reverse_playlist_items(self):
        reverse_items = self.config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if not isinstance(reverse_items, dict):
            reverse_items = {}
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict):
            library = {}
        changed = False
        normalized = {}
        for playlist_name, playlist_items in library.items():
            items = reverse_items.get(playlist_name, [])
            if not isinstance(items, list):
                items = []
                changed = True
            playlist_set = set(self._normalize_playlist_items(playlist_items))
            filtered = [
                item for item in self._normalize_playlist_items(items)
                if item in playlist_set
            ]
            if filtered:
                normalized[playlist_name] = filtered
            if filtered != items:
                changed = True
        if set(reverse_items.keys()) - set(library.keys()):
            changed = True
        if normalized != reverse_items:
            self.config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = normalized
            changed = True
        return changed

    def _get_monitor_playlist_name(self, monitor_name):
        names = self._playlist_names()
        if not names:
            return None
        assignments = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if not isinstance(assignments, dict):
            assignments = {}
        playlist_name = assignments.get(monitor_name)
        if isinstance(playlist_name, str) and playlist_name in names:
            return playlist_name
        return names[0]

    def _sync_monitor_playlists_from_library(self):
        changed = False
        library_raw = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        normalized_library = {}
        if isinstance(library_raw, dict):
            for raw_name, raw_items in library_raw.items():
                if not isinstance(raw_name, str):
                    continue
                playlist_name = raw_name.strip()
                if not playlist_name:
                    continue
                normalized_library[playlist_name] = self._normalize_playlist_items(raw_items)
        if not normalized_library:
            normalized_library = {"Default": []}
            changed = True

        active_name = self.config.get(CONFIG_KEY_PLAYLIST_ACTIVE, "Default")
        if not isinstance(active_name, str) or active_name not in normalized_library:
            active_name = next(iter(normalized_library.keys()))
            changed = True

        monitor_names = list(self.monitors.get_monitors().keys())
        if "Default" not in monitor_names:
            monitor_names.append("Default")

        assignments_raw = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        assignments = assignments_raw if isinstance(assignments_raw, dict) else {}
        monitor_playlists_raw = self.config.get(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        monitor_playlists = monitor_playlists_raw if isinstance(monitor_playlists_raw, dict) else {}
        normalized_assignments = {}

        for monitor_name in monitor_names:
            playlist_name = assignments.get(monitor_name)
            if not isinstance(playlist_name, str) or playlist_name not in normalized_library:
                legacy_items = self._normalize_playlist_items(monitor_playlists.get(monitor_name, []))
                playlist_name = None
                if legacy_items:
                    for existing_name, existing_items in normalized_library.items():
                        if existing_items == legacy_items:
                            playlist_name = existing_name
                            break
                    if playlist_name is None:
                        base = "Default" if monitor_name == "Default" else f"{monitor_name} Playlist"
                        playlist_name = self._unique_playlist_name(base, normalized_library.keys())
                        normalized_library[playlist_name] = legacy_items
                        changed = True
                if playlist_name is None:
                    playlist_name = active_name
                changed = True
            normalized_assignments[monitor_name] = playlist_name

        for monitor_name, playlist_name in assignments.items():
            if monitor_name in normalized_assignments:
                continue
            if not isinstance(monitor_name, str):
                continue
            if isinstance(playlist_name, str) and playlist_name in normalized_library:
                normalized_assignments[monitor_name] = playlist_name

        derived_monitor_playlists = {}
        for monitor_name, playlist_name in normalized_assignments.items():
            derived_monitor_playlists[monitor_name] = list(
                normalized_library.get(playlist_name, [])
            )

        active_items = list(normalized_library.get(active_name, []))
        if self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY) != normalized_library:
            self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = normalized_library
            changed = True
        if self.config.get(CONFIG_KEY_PLAYLIST_ACTIVE) != active_name:
            self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = active_name
            changed = True
        if self.config.get(CONFIG_KEY_PLAYLIST_SELECTION) != active_items:
            self.config[CONFIG_KEY_PLAYLIST_SELECTION] = active_items
            changed = True
        if self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS) != normalized_assignments:
            self.config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = normalized_assignments
            changed = True
        if self.config.get(CONFIG_KEY_MONITOR_PLAYLISTS) != derived_monitor_playlists:
            self.config[CONFIG_KEY_MONITOR_PLAYLISTS] = derived_monitor_playlists
            changed = True

        if self._normalize_reverse_playlist_items():
            changed = True

        return changed

    def _server_call_async(self, fn_name, *args):
        def _worker():
            try:
                server = get_service(DBUS_NAME_SERVER)
                self.server = server
                if server is None:
                    raise RuntimeError("server unavailable")
                getattr(server, fn_name)(*args)
            except Exception as e:
                logger.warning(f"[GUI] Server call {fn_name}({args}) error: {e}")
        threading.Thread(target=_worker, daemon=True).start()

    def _server_set_async(self, prop_name, value):
        def _worker():
            try:
                server = get_service(DBUS_NAME_SERVER)
                self.server = server
                if server is None:
                    raise RuntimeError("server unavailable")
                setattr(server, prop_name, value)
            except Exception as e:
                logger.warning(f"[GUI] Server prop {prop_name}={value} error: {e}")
        threading.Thread(target=_worker, daemon=True).start()

    # ──────────────────────────────────────────────────
    #  Style helpers
    # ──────────────────────────────────────────────────
    @staticmethod
    def _add_style_class(widget, cls):
        if widget is None:
            return
        ctx = widget.get_style_context()
        if not ctx.has_class(cls):
            ctx.add_class(cls)

    def _is_system_dark(self):
        settings = Gtk.Settings.get_default()
        if settings is None:
            return False
        theme_name = str(settings.get_property("gtk-theme-name") or "").lower()
        prefer_dark = bool(settings.get_property("gtk-application-prefer-dark-theme"))
        return prefer_dark or ("dark" in theme_name)

    def _effective_theme(self, theme):
        if theme == "system":
            return "dark" if self._is_system_dark() else "light"
        return "dark" if theme == "dark" else "light"

    def _apply_theme_css(self, theme):
        css = NEON_CSS_DARK if self._effective_theme(theme) == "dark" else NEON_CSS_LIGHT
        try:
            self._css_provider.load_from_data(css.encode("utf-8"))
        except TypeError:
            self._css_provider.load_from_data(css)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, self._css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _setup_neon_style_targets(self):
        if self.window is None:
            return
        self._add_style_class(self.window, "wb-root")
        self._add_style_class(self.window.get_titlebar(), "wb-header")
        self._add_style_class(self.builder.get_object("stack1"), "wb-panel")
        self._add_style_class(self.builder.get_object("PopoverMain"), "wb-popover")
        self._add_style_class(self.builder.get_object("IconView"), "wb-icon-grid")
        self._add_style_class(self.builder.get_object("StackSwitcher"), "wb-tab-switcher")
        for widget_id in (
            "BtnRefresh",
            "BtnOpenFolder",
            "ToggleFileManagerMode",
            "ButtonPlayPause",
            "ButtonFeelingLucky",
            "BtnAddToPlaylist",
        ):
            self._add_style_class(self.builder.get_object(widget_id), "wb-chip")

    # ──────────────────────────────────────────────────
    #  GPU status + video grid helpers
    # ──────────────────────────────────────────────────
    def _start_gpu_status_updates(self):
        if self._gpu_status_timer_id is not None:
            return
        self._update_gpu_status_label()
        self._gpu_status_timer_id = GLib.timeout_add_seconds(3, self._update_gpu_status_label)

    def _format_gpu_usage(self, snapshot):
        if not snapshot:
            return "usage n/a"
        parts = []
        for item in snapshot:
            name = item.get("name") or item.get("vendor", "gpu")
            usage = item.get("usage_percent")
            mem_used = item.get("memory_used_mb")
            mem_total = item.get("memory_total_mb")
            if usage is None:
                continue
            if mem_used is not None and mem_total:
                parts.append(f"{name}: {usage}% ({mem_used}/{mem_total}MB)")
            else:
                parts.append(f"{name}: {usage}%")
        return " | ".join(parts) if parts else "usage n/a"

    def _update_gpu_status_label(self):
        # Opt 3: run the external tool calls (nvidia-smi, lspci, glxinfo) off the GTK
        # main thread so they cannot stall UI rendering by 1-4 seconds.
        label: Gtk.Label = self.builder.get_object("LabelGpuStatus")
        if label is None:
            return True

        def _worker():
            try:
                if self._gpu_profile is None:
                    self._gpu_profile = get_vlc_hwdec_profile(force_refresh=True)
                snapshot = get_gpu_usage_snapshot()
                decode_mode = self._gpu_profile.get("hwdec", "none")
                decode_path = "GPU" if self._gpu_profile.get("gpu_available") else "CPU fallback"
                usage_text = self._format_gpu_usage(snapshot)
                vendors = ", ".join(self._gpu_profile.get("vendors") or ["unknown"])
                text = f"Decode: {decode_mode} ({decode_path}) | GPU: {vendors} | Usage: {usage_text}"
            except Exception as e:
                text = f"GPU status unavailable: {e}"

            def _update():
                try:
                    lbl = self.builder.get_object("LabelGpuStatus")
                    if lbl is not None:
                        lbl.set_text(text)
                except Exception:
                    pass
                return False

            GLib.idle_add(_update)

        threading.Thread(target=_worker, daemon=True, name="wb-gpu-status").start()
        return True

    def _on_stack_visible_child_changed(self, stack, _param):
        if stack.get_visible_child_name() == "video":
            self._start_video_grid_playback()
            self._schedule_video_grid_warmup()
        else:
            self._stop_video_grid_playback()

    def _is_video_tab_visible(self):
        stack: Gtk.Stack = self.builder.get_object("stack1")
        return stack is not None and stack.get_visible_child_name() == "video"

    def _schedule_video_grid_warmup(self):
        if self._video_grid_warmup_source_id is not None:
            return
        self._video_grid_warmup_source_id = GLib.timeout_add(
            self._THUMBNAIL_WARMUP_INTERVAL_MS,
            self._start_video_grid_playback_once,
        )

    def _start_video_grid_playback_once(self):
        if not self._is_video_tab_visible():
            self._video_grid_warmup_source_id = None
            return False
        has_pending = self._start_video_grid_playback()
        if not has_pending:
            self._video_grid_warmup_source_id = None
        return has_pending

    def _clear_video_grid(self):
        if self._video_grid_warmup_source_id is not None:
            try:
                GLib.source_remove(self._video_grid_warmup_source_id)
            except Exception:
                pass
            self._video_grid_warmup_source_id = None

        for tile in self._video_tiles.values():
            cleanup = getattr(tile, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    pass
        self._video_tiles.clear()

        if self.icon_view is not None:
            for child in list(self.icon_view.get_children()):
                try:
                    self.icon_view.remove(child)
                except Exception:
                    pass

    def _start_video_grid_playback(self):
        started = 0
        has_pending = False
        for tile in self._video_tiles.values():
            if getattr(tile, "_thumb_requested", False):
                continue
            has_pending = True
            if started >= self._THUMBNAIL_WARMUP_MAX:
                break
            start = getattr(tile, "start", None)
            if callable(start) and start():
                started += 1
        return has_pending

    def _stop_video_grid_playback(self):
        if self._video_grid_warmup_source_id is not None:
            try:
                GLib.source_remove(self._video_grid_warmup_source_id)
            except Exception:
                pass
            self._video_grid_warmup_source_id = None
        for tile in self._video_tiles.values():
            stop = getattr(tile, "stop", None)
            if callable(stop):
                stop()

    def _release_video_grid_instance(self):
        self._clear_video_grid()

    # ──────────────────────────────────────────────────
    #  Context menu (right-click set for monitor)
    # ──────────────────────────────────────────────────
    def _setup_context_menu(self):
        self.contextMenu_monitors = Gtk.Menu()
        for monitor_name, monitor_obj in self.monitors.get_monitors().items():
            item = Gtk.MenuItem(label=f"Set For {monitor_name}")
            item.connect("activate", self.on_set_as, monitor_obj)
            self.contextMenu_monitors.append(item)
        item_all = Gtk.MenuItem(label="Set For All")
        item_all.connect("activate", self.on_set_as, self.all_key)
        self.contextMenu_monitors.append(item_all)
        # Per-monitor: add to playlist sub-menu items
        self._build_add_to_playlist_menu()
        self._build_file_manager_menu()
        self.contextMenu_monitors.show_all()
        self._update_context_menu_state()

    def _build_add_to_playlist_menu(self):
        """Build 'Add to Playlist for <monitor>' sub-items."""
        sep = Gtk.SeparatorMenuItem()
        self.contextMenu_monitors.append(sep)
        for monitor_name in self.monitors.get_monitors():
            item = Gtk.MenuItem(label=f"Add to Playlist: {monitor_name}")
            item.connect("activate", self._on_add_to_monitor_playlist, monitor_name)
            self.contextMenu_monitors.append(item)

    def _append_context_item(self, label, callback):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        self.contextMenu_monitors.append(item)
        return item

    def _build_file_manager_menu(self):
        sep = Gtk.SeparatorMenuItem()
        self.contextMenu_monitors.append(sep)

        header = Gtk.MenuItem(label="File Manager")
        header.set_sensitive(False)
        self.contextMenu_monitors.append(header)

        self._fm_context_items["play"] = self._append_context_item(
            "Play (Default Player)", self.on_fm_play_selected
        )
        self._fm_context_items["open"] = self._append_context_item(
            "Open", self.on_fm_open_selected
        )
        self._fm_context_items["open_folder"] = self._append_context_item(
            "Open Containing Folder", self.on_fm_open_containing_folder
        )
        self.contextMenu_monitors.append(Gtk.SeparatorMenuItem())

        self._fm_context_items["copy"] = self._append_context_item(
            "Copy", self.on_fm_copy_selected
        )
        self._fm_context_items["cut"] = self._append_context_item(
            "Cut", self.on_fm_cut_selected
        )
        self._fm_context_items["paste"] = self._append_context_item(
            "Paste Into Wall Blazer Folder", self.on_fm_paste_selected
        )
        self.contextMenu_monitors.append(Gtk.SeparatorMenuItem())

        self._fm_context_items["rename"] = self._append_context_item(
            "Rename", self.on_fm_rename_selected
        )
        self._fm_context_items["delete"] = self._append_context_item(
            "Delete", self.on_fm_delete_selected
        )
        self.contextMenu_monitors.append(Gtk.SeparatorMenuItem())

        self._fm_context_items["properties"] = self._append_context_item(
            "Properties", self.on_fm_properties_selected
        )
        self._fm_context_items["refresh"] = self._append_context_item(
            "Refresh", self._reload_icon_view
        )

    def _update_context_menu_state(self):
        selected_count = len(self._get_selected_video_paths())
        enabled = bool(self._file_manager_mode)
        for item in self._fm_context_items.values():
            item.set_sensitive(enabled)
        if not enabled:
            return

        def _set(name, state):
            item = self._fm_context_items.get(name)
            if item is not None:
                item.set_sensitive(state)

        _set("play", selected_count >= 1)
        _set("open", selected_count >= 1)
        _set("open_folder", selected_count >= 1)
        _set("copy", selected_count >= 1)
        _set("cut", selected_count >= 1)
        _set("rename", selected_count == 1)
        _set("delete", selected_count >= 1)
        _set("properties", selected_count >= 1)
        _set("paste", bool(self._fm_clipboard_paths))
        _set("refresh", True)

    # ──────────────────────────────────────────────────
    #  GTK Application lifecycle
    # ──────────────────────────────────────────────────
    def do_startup(self):
        Gtk.Application.do_startup(self)
        apply_gtk_theme(self.config.get(CONFIG_KEY_THEME, "system"))
        self._apply_theme_css(self.config.get(CONFIG_KEY_THEME, "system"))

        # Simple (non-stateful) actions — wired to new button IDs
        simple_actions = [
            ("open_config", lambda *_: open_path_default(os.path.realpath(CONFIG_PATH))),
            ("about", self.on_about),
            ("quit", self.on_quit),
        ]
        for name, handler in simple_actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        # Connect buttons in the new UI by ID (no action-name in XML)
        self._connect_button("BtnOpenConfig", self.on_open_config)
        self._connect_button("BtnAbout", self.on_about)
        self._connect_button("BtnQuit", self.on_quit)
        self._connect_button("BtnRefresh", self._reload_icon_view)
        self._connect_button("BtnOpenFolder", lambda *_: open_path_default(
            os.path.realpath(VIDEO_WALLPAPER_DIR)))
        self._connect_button("ButtonApply", self.on_local_video_apply)
        self._connect_button("ButtonApply2", self.on_local_web_page_apply)
        self._connect_button("ButtonPlayPause", self.on_play_pause)
        self._connect_button("ButtonFeelingLucky", self.on_feeling_lucky)
        self._connect_button("BtnAddToPlaylist", self.on_add_to_playlist_clicked)
        self._connect_button("BtnPlaylistRemove", self.on_playlist_remove_clicked)
        self._connect_button("BtnPlaylistClear", self.on_playlist_clear_clicked)
        self._connect_button("BtnPlaylistNew", self.on_playlist_new_clicked)
        self._connect_button("BtnPlaylistDelete", self.on_playlist_delete_clicked)
        toggle_file_manager_mode: Gtk.ToggleButton = self.builder.get_object("ToggleFileManagerMode")
        if toggle_file_manager_mode is not None:
            toggle_file_manager_mode.set_active(self._file_manager_mode)
            toggle_file_manager_mode.connect("toggled", self.on_file_manager_mode_toggled)
        entry_video_search: Gtk.Entry = self.builder.get_object("EntryVideoSearch")
        if entry_video_search is not None:
            entry_video_search.connect("changed", self.on_video_search_changed)

        # Wire CheckButtons in Python (NO action-name in XML)
        self._wire_check("ToggleAutostart", self.is_autostart, self.on_autostart_toggled)
        self._wire_check("ToggleStaticWallpaper",
                         self.config.get(CONFIG_KEY_STATIC_WALLPAPER, True),
                         self.on_static_wallpaper_toggled)
        self._wire_check("TogglePauseWhenMaximized",
                         self.config.get(CONFIG_KEY_PAUSE_WHEN_MAXIMIZED, True),
                         self.on_pause_when_maximized_toggled)
        self._wire_check("ToggleMuteWhenMaximized",
                         self.config.get(CONFIG_KEY_MUTE_WHEN_MAXIMIZED, False),
                         self.on_mute_when_maximized_toggled)
        self._wire_check("TogglePlaylist",
                         self.config.get(CONFIG_KEY_PLAYLIST, False),
                         self.on_playlist_toggled)
        self._wire_check("TogglePlaylistShuffle",
                         self.config.get(CONFIG_KEY_PLAYLIST_SHUFFLE, False),
                         self.on_playlist_shuffle_toggled)
        self._wire_check("ToggleReverseSingle",
                         self.config.get(CONFIG_KEY_REVERSE_SINGLE, False),
                         self.on_reverse_single_toggled)
        self._wire_check("TogglePlaylistReverse",
                         self.config.get(CONFIG_KEY_REVERSE_PLAYLIST, False),
                         self.on_playlist_reverse_all_toggled)

        # ToggleMute (GtkToggleButton, not CheckButton)
        toggle_mute: Gtk.ToggleButton = self.builder.get_object("ToggleMute")
        if toggle_mute:
            toggle_mute.set_active(self.config.get(CONFIG_KEY_MUTE, False))
            toggle_mute.connect("toggled", self.on_mute_toggled)

        # Wayland/non-GNOME visibility
        if is_wayland():
            self._set_visible("TogglePauseWhenMaximized", False)
            self._set_visible("ToggleMuteWhenMaximized", False)
        if not is_gnome():
            self._set_visible("ToggleStaticWallpaper", False)
            self._set_visible("LabelBlurRadius", False)
            self._set_visible("SpinBlurRadius", False)

        # Playlist interval spinner (0 = advance when video ends, >0 = timer in minutes)
        spin = self.builder.get_object("SpinPlaylistInterval")
        if spin:
            interval_sec = max(0, int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300)))
            adj = spin.get_adjustment()
            if adj:
                adj.set_lower(0)
                adj.set_step_increment(1)
            spin.set_value(interval_sec / 60 if interval_sec > 0 else 0)
            spin.set_tooltip_text(
                "Minutes between playlist advances.\n"
                "0 = advance automatically when each video ends"
            )
            spin.connect("value-changed", self.on_playlist_interval_changed)

        # Theme combo
        combo_theme = self.builder.get_object("ComboTheme")
        if combo_theme:
            theme_map = {"system": 0, "dark": 1, "light": 2}
            combo_theme.set_active(theme_map.get(self.config.get(CONFIG_KEY_THEME, "system"), 0))
            combo_theme.connect("changed", self.on_theme_changed)

        # Build playlist monitor list
        if self._sync_monitor_playlists_from_library():
            self._save_config()
        self._build_monitor_listbox()
        self._setup_playlist_selector()
        # Setup TreeViewPlaylist columns
        self._setup_playlist_treeview()
        if self._playlist_selected_monitor is not None:
            self._refresh_playlist_treeview(self._playlist_selected_monitor)

        self._reload_icon_view()
        self._start_gpu_status_updates()
        stack: Gtk.Stack = self.builder.get_object("stack1")
        if stack is not None:
            stack.connect("notify::visible-child-name", self._on_stack_visible_child_changed)
            self._on_stack_visible_child_changed(stack, None)
        self.set_mute_toggle_icon()
        self.set_scale_volume_sensitive()
        self.set_spin_blur_radius_sensitive()

        # Volume scale
        scale_volume = self.builder.get_object("ScaleVolume")
        adj_volume = self.builder.get_object("AdjustmentVolume")
        if adj_volume and scale_volume:
            adj_volume.handler_block_by_func(self.on_volume_changed)
            scale_volume.set_value(self.config.get(CONFIG_KEY_VOLUME, 50))
            adj_volume.handler_unblock_by_func(self.on_volume_changed)

        # Blur radius spin
        spin_blur = self.builder.get_object("SpinBlurRadius")
        adj_blur = self.builder.get_object("AdjustmentBlur")
        if adj_blur and spin_blur:
            adj_blur.handler_block_by_func(self.on_blur_radius_changed)
            spin_blur.set_value(self.config.get(CONFIG_KEY_BLUR_RADIUS, 5))
            adj_blur.handler_unblock_by_func(self.on_blur_radius_changed)

        self._setup_advanced_video_controls()

    def _connect_button(self, widget_id, handler):
        widget = self.builder.get_object(widget_id)
        if widget is None:
            return
        widget.connect("clicked", handler)

    def _wire_check(self, widget_id, initial_value, handler):
        widget: Gtk.CheckButton = self.builder.get_object(widget_id)
        if widget is None:
            return
        widget.set_active(bool(initial_value))
        widget.connect("toggled", handler)

    def _set_visible(self, widget_id, visible):
        w = self.builder.get_object(widget_id)
        if w:
            w.set_visible(visible)

    def _queue_video_profile_push(self):
        if self.config.get(CONFIG_KEY_MODE) != MODE_VIDEO:
            return
        if self._video_profile_push_source_id is not None:
            try:
                GLib.source_remove(self._video_profile_push_source_id)
            except Exception:
                pass
        self._video_profile_push_source_id = GLib.timeout_add(
            180, self._push_video_profile_now
        )

    def _push_video_profile_now(self):
        self._video_profile_push_source_id = None
        self._server_call_async("apply_video_profile")
        return False

    def _video_adjustments(self):
        values = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        raw = self.config.get(CONFIG_KEY_VIDEO_ADJUSTMENTS, {})
        if isinstance(raw, dict):
            for key in values.keys():
                if key in raw:
                    try:
                        values[key] = float(raw[key])
                    except (TypeError, ValueError):
                        pass
        return values

    def _create_scale_row(self, label, minimum, maximum, step, digits, initial, handler, tooltip=None):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title = Gtk.Label(label=label)
        title.set_xalign(0)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, minimum, maximum, step)
        scale.set_digits(digits)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        scale.set_value(initial)
        if tooltip:
            scale.set_tooltip_text(tooltip)
        scale.connect("value-changed", handler)
        row.pack_start(title, False, False, 0)
        row.pack_start(scale, False, True, 0)
        return row, scale

    def _setup_advanced_video_controls(self):
        if self._advanced_widgets:
            return
        container = self.builder.get_object("PopoverBox")
        popover = self.builder.get_object("PopoverMain")
        if container is None:
            return
        if popover is not None:
            popover.set_size_request(460, 720)

        container.pack_start(Gtk.Separator(), False, True, 0)

        playback_expander = Gtk.Expander(label="Playback & Fit")
        playback_expander.set_expanded(False)
        playback_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        playback_box.set_margin_top(6)

        fit_label = Gtk.Label(label="Fit mode")
        fit_label.set_xalign(0)
        fit_combo = Gtk.ComboBoxText()
        fit_combo.append("cover", "Cover (Fill Screen)")
        fit_combo.append("contain", "Contain (Show Whole Video)")
        fit_combo.append("stretch", "Stretch")
        fit_combo.connect("changed", self.on_video_fit_mode_changed)
        playback_box.pack_start(fit_label, False, False, 0)
        playback_box.pack_start(fit_combo, False, False, 0)
        self._advanced_widgets["fit_mode"] = fit_combo

        speed_specs = [
            ("Single Wallpaper Speed", CONFIG_KEY_PLAYBACK_SPEED_SINGLE, "speed_single"),
            ("Playlist Forward Speed", CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST, "speed_playlist"),
            ("Reverse Speed", CONFIG_KEY_PLAYBACK_SPEED_REVERSE, "speed_reverse"),
        ]
        for label, key, widget_key in speed_specs:
            row, scale = self._create_scale_row(
                label,
                0.25,
                4.0,
                0.25,
                2,
                float(self.config.get(key, 1.0)),
                lambda scale, cfg_key=key: self.on_playback_speed_changed(scale, cfg_key),
                tooltip="Playback speed from 0.25x to 4x",
            )
            playback_box.pack_start(row, False, True, 0)
            self._advanced_widgets[widget_key] = scale

        playback_expander.add(playback_box)
        container.pack_start(playback_expander, False, True, 0)

        color_expander = Gtk.Expander(label="Video Color Tuning")
        color_expander.set_expanded(False)
        color_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        color_box.set_margin_top(6)
        adjustments = self._video_adjustments()
        scale_specs = [
            ("Brightness", "brightness", 0.2, 3.0, 0.05, 2),
            ("Contrast", "contrast", 0.2, 3.0, 0.05, 2),
            ("Saturation", "saturation", 0.0, 4.0, 0.05, 2),
            ("Gamma", "gamma", 0.2, 4.0, 0.05, 2),
            ("Hue Shift", "hue", -180.0, 180.0, 1.0, 0),
            ("Red Tint", "red", -1.0, 1.0, 0.02, 2),
            ("Green Tint", "green", -1.0, 1.0, 0.02, 2),
            ("Blue Tint", "blue", -1.0, 1.0, 0.02, 2),
            ("Yellow Tint", "yellow", -1.0, 1.0, 0.02, 2),
            ("Cyan Tint", "cyan", -1.0, 1.0, 0.02, 2),
            ("Magenta Tint", "magenta", -1.0, 1.0, 0.02, 2),
        ]
        for label, key, minimum, maximum, step, digits in scale_specs:
            row, scale = self._create_scale_row(
                label,
                minimum,
                maximum,
                step,
                digits,
                float(adjustments.get(key, DEFAULT_VIDEO_ADJUSTMENTS[key])),
                lambda scale, adj_key=key: self.on_video_adjustment_changed(scale, adj_key),
            )
            color_box.pack_start(row, False, True, 0)
            self._advanced_widgets[f"adj_{key}"] = scale

        reset_btn = Gtk.Button(label="Reset Video Colors")
        reset_btn.connect("clicked", self.on_reset_video_adjustments_clicked)
        color_box.pack_start(reset_btn, False, False, 0)

        color_expander.add(color_box)
        container.pack_start(color_expander, False, True, 0)
        container.show_all()
        self._sync_advanced_control_values()

    def _sync_advanced_control_values(self):
        if not self._advanced_widgets:
            return
        self._syncing_advanced_widgets = True
        try:
            fit_combo = self._advanced_widgets.get("fit_mode")
            if fit_combo is not None:
                fit_combo.set_active_id(
                    self.config.get(CONFIG_KEY_VIDEO_FIT_MODE, "cover")
                )
            for key, widget_key in (
                (CONFIG_KEY_PLAYBACK_SPEED_SINGLE, "speed_single"),
                (CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST, "speed_playlist"),
                (CONFIG_KEY_PLAYBACK_SPEED_REVERSE, "speed_reverse"),
            ):
                widget = self._advanced_widgets.get(widget_key)
                if widget is not None:
                    widget.set_value(float(self.config.get(key, 1.0)))
            adjustments = self._video_adjustments()
            for adj_key, value in adjustments.items():
                widget = self._advanced_widgets.get(f"adj_{adj_key}")
                if widget is not None:
                    widget.set_value(float(value))
        finally:
            self._syncing_advanced_widgets = False

    def on_video_fit_mode_changed(self, combo):
        if self._syncing_advanced_widgets:
            return
        fit_mode = combo.get_active_id() or "cover"
        self.config[CONFIG_KEY_VIDEO_FIT_MODE] = fit_mode
        self._save_config_delay()
        self._queue_video_profile_push()

    def on_playback_speed_changed(self, scale, config_key):
        if self._syncing_advanced_widgets:
            return
        value = round(scale.get_value() / 0.25) * 0.25
        if abs(scale.get_value() - value) > 1e-6:
            scale.set_value(value)
            return
        self.config[config_key] = float(value)
        self._save_config_delay()
        self._queue_video_profile_push()

    def on_video_adjustment_changed(self, scale, adjustment_key):
        if self._syncing_advanced_widgets:
            return
        adjustments = self._video_adjustments()
        value = scale.get_value()
        if adjustment_key == "hue":
            value = float(round(value))
            if abs(scale.get_value() - value) > 1e-6:
                scale.set_value(value)
                return
        else:
            value = round(float(value), 3)
        adjustments[adjustment_key] = value
        self.config[CONFIG_KEY_VIDEO_ADJUSTMENTS] = adjustments
        self._save_config_delay()
        self._queue_video_profile_push()

    def on_reset_video_adjustments_clicked(self, *_):
        self.config[CONFIG_KEY_VIDEO_ADJUSTMENTS] = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        self._save_config()
        self._sync_advanced_control_values()
        self._queue_video_profile_push()

    def do_activate(self):
        if self.window is None:
            self.window: Gtk.ApplicationWindow = self.builder.get_object("ApplicationWindow")
            self.window.set_title("Wall Blazer")
            self.window.set_application(self)
            self.window.set_position(Gtk.WindowPosition.CENTER)
            self._setup_neon_style_targets()
            self._apply_theme_css(self.config.get(CONFIG_KEY_THEME, "system"))
        self.window.present()
        if self.server is None:
            self._show_error("Couldn't connect to server")
        if self.config.get(CONFIG_KEY_FIRST_TIME, False):
            self._show_welcome()
            self.config[CONFIG_KEY_FIRST_TIME] = False
            self._save_config()

    # ──────────────────────────────────────────────────
    #  Per-Monitor Playlist – ListBox (left side)
    # ──────────────────────────────────────────────────
    def _setup_playlist_selector(self):
        combo: Gtk.ComboBoxText = self.builder.get_object("ComboMonitorPlaylist")
        if combo is None:
            return
        if self._playlist_combo_handler_id is None:
            self._playlist_combo_handler_id = combo.connect(
                "changed", self.on_monitor_playlist_changed
            )
        self._refresh_playlist_selector()

    def _refresh_playlist_selector(self):
        combo: Gtk.ComboBoxText = self.builder.get_object("ComboMonitorPlaylist")
        if combo is None:
            return

        if self._playlist_combo_handler_id is not None:
            combo.handler_block(self._playlist_combo_handler_id)
        combo.remove_all()
        playlist_names = self._playlist_names()
        for playlist_name in playlist_names:
            combo.append(playlist_name, playlist_name)

        selected_monitor = self._playlist_selected_monitor
        if selected_monitor is None:
            monitors = list(self.monitors.get_monitors().keys())
            selected_monitor = monitors[0] if monitors else "Default"
        playlist_name = self._get_monitor_playlist_name(selected_monitor)
        if playlist_name in playlist_names:
            combo.set_active_id(playlist_name)
        elif playlist_names:
            combo.set_active(0)
        if self._playlist_combo_handler_id is not None:
            combo.handler_unblock(self._playlist_combo_handler_id)

    def _build_monitor_listbox(self):
        listbox: Gtk.ListBox = self.builder.get_object("ListBoxMonitors")
        if listbox is None:
            return
        # Clear existing rows
        for child in listbox.get_children():
            listbox.remove(child)
        # Add one row per monitor
        monitor_names = list(self.monitors.get_monitors().keys())
        if not monitor_names:
            monitor_names = ["Default"]
        for name in monitor_names:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=name)
            label.set_xalign(0)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            row.add(label)
            row.set_name(name)  # store monitor name as widget name
            row.show_all()
            listbox.add(row)
        # Bug 2: only connect row-selected once; disconnect old handler before re-connecting.
        if self._monitor_listbox_handler_id is not None:
            try:
                listbox.disconnect(self._monitor_listbox_handler_id)
            except Exception:
                pass
            self._monitor_listbox_handler_id = None
        self._monitor_listbox_handler_id = listbox.connect(
            "row-selected", self._on_monitor_row_selected
        )
        # Select first by default
        if listbox.get_children():
            first_row = listbox.get_row_at_index(0)
            listbox.select_row(first_row)

    def _on_monitor_row_selected(self, listbox, row):
        if row is None:
            return
        monitor_name = row.get_name()
        self._playlist_selected_monitor = monitor_name
        self._refresh_playlist_selector()
        self._refresh_playlist_treeview(monitor_name)
        self._sync_icon_view_selection_to_monitor_playlist(monitor_name)

    # ──────────────────────────────────────────────────
    #  Per-Monitor Playlist – TreeView (right side)
    # ──────────────────────────────────────────────────
    def _setup_playlist_treeview(self):
        tv: Gtk.TreeView = self.builder.get_object("TreeViewPlaylist")
        if tv is None:
            return
        # Two columns: #, Filename
        self._playlist_store = Gtk.ListStore(int, str, str, bool)  # index, basename, fullpath, reverse
        tv.set_model(self._playlist_store)

        # Column: #
        renderer_num = Gtk.CellRendererText()
        col_num = Gtk.TreeViewColumn("#", renderer_num, text=0)
        col_num.set_min_width(40)
        tv.append_column(col_num)

        # Column: Video Name
        renderer_name = Gtk.CellRendererText()
        renderer_name.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
        col_name = Gtk.TreeViewColumn("Video", renderer_name, text=1)
        col_name.set_expand(True)
        tv.append_column(col_name)

        # Column: Reverse toggle
        renderer_reverse = Gtk.CellRendererToggle()
        renderer_reverse.set_property("activatable", True)
        renderer_reverse.connect("toggled", self.on_playlist_reverse_toggled)
        col_reverse = Gtk.TreeViewColumn("Reverse", renderer_reverse, active=3)
        col_reverse.set_min_width(70)
        tv.append_column(col_reverse)

        # Column: Full Path
        renderer_path = Gtk.CellRendererText()
        renderer_path.set_property("ellipsize", 3)
        renderer_path.set_property("sensitive", False)
        col_path = Gtk.TreeViewColumn("Path", renderer_path, text=2)
        col_path.set_expand(True)
        tv.append_column(col_path)

        # Enable drag-and-drop reorder
        tv.set_reorderable(True)
        self._playlist_store.connect("row-inserted", self._on_playlist_reordered)
        self._playlist_store.connect("row-deleted", self._on_playlist_reordered)

    def _refresh_playlist_treeview(self, monitor_name):
        if self._playlist_store is None:
            return
        playlist_name = self._get_monitor_playlist_name(monitor_name)
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        videos = []
        if isinstance(library, dict) and isinstance(playlist_name, str):
            videos = library.get(playlist_name, []) or []
        videos = self._normalize_playlist_items(videos)
        reverse_set = self._reverse_items_for_playlist(playlist_name)
        self._playlist_store.clear()
        for i, path in enumerate(videos):
            self._playlist_store.append([i + 1, os.path.basename(path), path, path in reverse_set])
        lbl: Gtk.Label = self.builder.get_object("LabelPlaylistHeader")
        if lbl:
            monitor_txt = GLib.markup_escape_text(str(monitor_name))
            playlist_txt = GLib.markup_escape_text(str(playlist_name or "—"))
            lbl.set_markup(f"<b>Playlist for: {monitor_txt} ({playlist_txt})</b>")

    def _get_current_playlist_from_store(self):
        """Return ordered list of video paths from the current tree store."""
        if self._playlist_store is None:
            return []
        return [row[2] for row in self._playlist_store]

    def _sync_icon_view_selection_to_monitor_playlist(self, monitor_name=None):
        if self.icon_view is None or not self.video_paths:
            return
        if monitor_name is None:
            monitor_name = self._playlist_selected_monitor
        if monitor_name is None:
            return
        playlist_name = self._get_monitor_playlist_name(monitor_name)
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        videos = []
        if isinstance(library, dict) and isinstance(playlist_name, str):
            videos = library.get(playlist_name, []) or []
        selected_set = set(self._normalize_playlist_items(videos))
        if isinstance(self.icon_view, Gtk.FlowBox):
            self.icon_view.unselect_all()
            for video_path in self.video_paths:
                if video_path in selected_set:
                    child = self._video_tiles.get(video_path)
                    if child is not None:
                        self.icon_view.select_child(child)
            self._on_icon_view_selection_changed(self.icon_view)

    def _on_playlist_reordered(self, store, *_args):
        """Save reordered playlist back to config (debounced)."""
        if self._playlist_selected_monitor is None:
            return
        playlist_name = self._get_monitor_playlist_name(self._playlist_selected_monitor)
        if not playlist_name:
            return
        videos = self._get_current_playlist_from_store()
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict):
            library = {}
        library[playlist_name] = self._normalize_playlist_items(videos)
        self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = library
        self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = playlist_name
        self._sync_monitor_playlists_from_library()
        self._prune_reverse_items_for_playlist(playlist_name, videos)
        # Renumber #
        for i, row in enumerate(self._playlist_store):
            self._playlist_store.set_value(row.iter, 0, i + 1)
        self._save_config_delay()
        if self.config.get(CONFIG_KEY_PLAYLIST, False):
            self._server_call_async("reload")

    # ──────────────────────────────────────────────────
    #  Playlist button handlers
    # ──────────────────────────────────────────────────
    def on_monitor_playlist_changed(self, combo):
        if self._playlist_selected_monitor is None:
            return
        playlist_name = combo.get_active_id() or combo.get_active_text()
        if not playlist_name:
            return
        assignments = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if not isinstance(assignments, dict):
            assignments = {}
        assignments[self._playlist_selected_monitor] = playlist_name
        self.config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = assignments
        self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = playlist_name
        self._sync_monitor_playlists_from_library()
        self._save_config()
        self._refresh_playlist_treeview(self._playlist_selected_monitor)
        self._sync_icon_view_selection_to_monitor_playlist(self._playlist_selected_monitor)
        if self.config.get(CONFIG_KEY_PLAYLIST, False):
            self._server_call_async("reload")

    def on_playlist_new_clicked(self, *_):
        dialog = Gtk.Dialog(
            title="Create Playlist",
            transient_for=self.window,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "Create", Gtk.ResponseType.OK,
        )
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.add(Gtk.Label(label="Playlist name:"))
        entry = Gtk.Entry()
        entry.set_placeholder_text("e.g. Chill Loop")
        entry.set_activates_default(True)
        content.add(entry)
        dialog.show_all()
        response = dialog.run()
        playlist_name = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        if not playlist_name:
            self._show_error("Playlist name cannot be empty.")
            return
        existing_names = self._playlist_names()
        if playlist_name in existing_names:
            self._show_error(f"Playlist '{playlist_name}' already exists.")
            return

        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict):
            library = {}
        library[playlist_name] = []
        self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = library
        self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = playlist_name

        monitor_name = self._playlist_selected_monitor
        if monitor_name is None:
            monitors = list(self.monitors.get_monitors().keys())
            monitor_name = monitors[0] if monitors else "Default"
            self._playlist_selected_monitor = monitor_name
        assignments = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if not isinstance(assignments, dict):
            assignments = {}
        assignments[monitor_name] = playlist_name
        self.config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = assignments
        self._sync_monitor_playlists_from_library()
        self._save_config()
        self._refresh_playlist_selector()
        self._refresh_playlist_treeview(monitor_name)
        self._sync_icon_view_selection_to_monitor_playlist(monitor_name)
        if self.config.get(CONFIG_KEY_PLAYLIST, False):
            self._server_call_async("reload")

    def on_playlist_delete_clicked(self, *_):
        names = self._playlist_names()
        if len(names) <= 1:
            self._show_error("At least one playlist must exist.")
            return
        combo: Gtk.ComboBoxText = self.builder.get_object("ComboMonitorPlaylist")
        if combo is None:
            return
        playlist_name = combo.get_active_id() or combo.get_active_text()
        if not playlist_name:
            return

        confirm = Gtk.MessageDialog(
            parent=self.window,
            modal=True,
            destroy_with_parent=True,
            text=f"Delete playlist '{playlist_name}'?",
            message_type=Gtk.MessageType.QUESTION,
            secondary_text="This removes only the playlist list, not your video files.",
            buttons=Gtk.ButtonsType.NONE,
        )
        confirm.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Delete", Gtk.ResponseType.OK)
        response = confirm.run()
        confirm.destroy()
        if response != Gtk.ResponseType.OK:
            return

        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict) or playlist_name not in library:
            return
        del library[playlist_name]
        reverse_items = self.config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if isinstance(reverse_items, dict) and playlist_name in reverse_items:
            reverse_items.pop(playlist_name, None)
            self.config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = reverse_items
        fallback = "Default" if "Default" in library else next(iter(library.keys()))
        assignments = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if not isinstance(assignments, dict):
            assignments = {}
        for monitor_name, assigned in list(assignments.items()):
            if assigned == playlist_name:
                assignments[monitor_name] = fallback
        self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = library
        self.config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = assignments
        if self.config.get(CONFIG_KEY_PLAYLIST_ACTIVE) == playlist_name:
            self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = fallback
        self._sync_monitor_playlists_from_library()
        self._save_config()
        self._refresh_playlist_selector()
        if self._playlist_selected_monitor:
            self._refresh_playlist_treeview(self._playlist_selected_monitor)
            self._sync_icon_view_selection_to_monitor_playlist(self._playlist_selected_monitor)
        if self.config.get(CONFIG_KEY_PLAYLIST, False):
            self._server_call_async("reload")

    def _on_add_to_monitor_playlist(self, _widget, monitor_name):
        """Add selected videos from IconView to a specific monitor's playlist."""
        selected = self._get_selected_video_paths()
        if not selected:
            self._show_error("Select one or more videos first.")
            return
        playlist_name = self._get_monitor_playlist_name(monitor_name)
        if not playlist_name:
            self._show_error("No playlist available.")
            return
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict):
            library = {}
        existing = self._normalize_playlist_items(library.get(playlist_name, []))
        # de-duplicate while preserving order
        seen = set(existing)
        for v in selected:
            if v not in seen:
                existing.append(v)
                seen.add(v)
        library[playlist_name] = existing
        self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = library
        self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = playlist_name
        self._sync_monitor_playlists_from_library()
        self._save_config()
        logger.info(
            f"[GUI] Added {len(selected)} videos to selected monitor playlist."
        )
        # If this monitor is currently visible in the tree, refresh it
        if self._playlist_selected_monitor == monitor_name:
            self._refresh_playlist_selector()
            self._refresh_playlist_treeview(monitor_name)
            self._sync_icon_view_selection_to_monitor_playlist(monitor_name)
        if self.config.get(CONFIG_KEY_PLAYLIST, False):
            self._server_call_async("reload")

    def on_add_to_playlist_clicked(self, *_):
        """Add selected videos to the currently selected monitor's playlist."""
        selected = self._get_selected_video_paths()
        if not selected:
            self._show_error("Select one or more videos from the Local Video tab first.")
            return
        monitor_name = self._playlist_selected_monitor
        if monitor_name is None:
            # Default to first monitor
            monitors = list(self.monitors.get_monitors().keys())
            monitor_name = monitors[0] if monitors else "Default"
        self._on_add_to_monitor_playlist(None, monitor_name)
        # Switch to Playlist tab
        stack: Gtk.Stack = self.builder.get_object("stack1")
        if stack:
            child = stack.get_child_by_name("playlist")
            if child:
                stack.set_visible_child(child)

    def on_playlist_remove_clicked(self, *_):
        """Remove selected rows from the TreeViewPlaylist."""
        tv: Gtk.TreeView = self.builder.get_object("TreeViewPlaylist")
        if tv is None or self._playlist_selected_monitor is None:
            return
        sel: Gtk.TreeSelection = tv.get_selection()
        model, paths = sel.get_selected_rows()
        if not paths:
            return
        # Remove in reverse order to avoid index shifting
        iters = [model.get_iter(p) for p in paths]
        for it in reversed(iters):
            model.remove(it)
        # Renumber
        for i, row in enumerate(self._playlist_store):
            self._playlist_store.set_value(row.iter, 0, i + 1)
        # Save
        playlist_name = self._get_monitor_playlist_name(self._playlist_selected_monitor)
        if not playlist_name:
            return
        videos = self._get_current_playlist_from_store()
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict):
            library = {}
        library[playlist_name] = self._normalize_playlist_items(videos)
        self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = library
        self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = playlist_name
        self._sync_monitor_playlists_from_library()
        self._prune_reverse_items_for_playlist(playlist_name, videos)
        self._save_config()
        self._sync_icon_view_selection_to_monitor_playlist(self._playlist_selected_monitor)
        if self.config.get(CONFIG_KEY_PLAYLIST, False):
            self._server_call_async("reload")

    def on_playlist_reverse_toggled(self, _cell, path_str):
        if self._playlist_store is None or self._playlist_selected_monitor is None:
            return
        playlist_name = self._get_monitor_playlist_name(self._playlist_selected_monitor)
        if not playlist_name:
            return
        model = self._playlist_store
        tree_iter = model.get_iter_from_string(path_str)
        if tree_iter is None:
            return
        current = bool(model.get_value(tree_iter, 3))
        new_value = not current
        model.set_value(tree_iter, 3, new_value)
        video_path = model.get_value(tree_iter, 2)

        reverse_items = self.config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if not isinstance(reverse_items, dict):
            reverse_items = {}
        items = self._normalize_playlist_items(reverse_items.get(playlist_name, []))
        if new_value:
            if video_path not in items:
                items.append(video_path)
        else:
            if video_path in items:
                items.remove(video_path)
        if items:
            reverse_items[playlist_name] = items
        else:
            reverse_items.pop(playlist_name, None)
        self.config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = reverse_items
        self._save_config()
        if self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
            self._server_call_async("reload")

    def on_playlist_clear_clicked(self, *_):
        """Clear the entire playlist for the selected monitor."""
        if self._playlist_selected_monitor is None:
            return
        playlist_name = self._get_monitor_playlist_name(self._playlist_selected_monitor)
        if not playlist_name:
            return
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(library, dict):
            library = {}
        library[playlist_name] = []
        self.config[CONFIG_KEY_PLAYLIST_LIBRARY] = library
        self.config[CONFIG_KEY_PLAYLIST_ACTIVE] = playlist_name
        self._sync_monitor_playlists_from_library()
        self._prune_reverse_items_for_playlist(playlist_name, [])
        self._save_config()
        if self._playlist_store:
            # Bug 5: block the row-deleted signal while clearing so _on_playlist_reordered
            # does not fire for each removed row and trigger spurious server.reload() calls.
            self._playlist_store.handler_block_by_func(self._on_playlist_reordered)
            try:
                self._playlist_store.clear()
            finally:
                self._playlist_store.handler_unblock_by_func(self._on_playlist_reordered)
        self._sync_icon_view_selection_to_monitor_playlist(self._playlist_selected_monitor)
        if self.config.get(CONFIG_KEY_PLAYLIST, False):
            self._server_call_async("reload")

    # ──────────────────────────────────────────────────
    #  Icon View helpers
    # ──────────────────────────────────────────────────
    def _get_filtered_video_paths(self):
        if not self._video_search_query:
            return list(self.video_paths)

        filtered = []
        for video_path in self.video_paths:
            lower_path = video_path.lower()
            lower_name = os.path.basename(video_path).lower()
            if self._video_search_query in lower_name or self._video_search_query in lower_path:
                filtered.append(video_path)
        return filtered

    def _rebuild_icon_view(self, selected_paths=None, sync_with_playlist=False):
        if self.icon_view is None:
            return
        if not isinstance(self.icon_view, Gtk.FlowBox):
            logger.error("[GUI] IconView widget is not GtkFlowBox")
            return

        self._clear_video_grid()

        self.icon_view.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.icon_view.set_row_spacing(12)
        self.icon_view.set_column_spacing(12)
        self.icon_view.set_activate_on_single_click(False)
        self.icon_view.set_max_children_per_line(6)
        self.icon_view.set_min_children_per_line(2)

        if self._icon_view_handler_id is not None:
            try:
                self.icon_view.disconnect(self._icon_view_handler_id)
            except Exception:
                pass
            self._icon_view_handler_id = None
        if self._icon_view_selection_handler_id is not None:
            try:
                self.icon_view.disconnect(self._icon_view_selection_handler_id)
            except Exception:
                pass
            self._icon_view_selection_handler_id = None
        if self._icon_view_key_handler_id is not None:
            try:
                self.icon_view.disconnect(self._icon_view_key_handler_id)
            except Exception:
                pass
            self._icon_view_key_handler_id = None

        for video_path in self._get_filtered_video_paths():
            tile = VideoGridTile(video_path)
            tile.video_path = video_path
            tile.event_box.connect("button-press-event", self.on_icon_tile_button_press, tile)
            self.icon_view.add(tile)
            self._video_tiles[video_path] = tile

        self._icon_view_handler_id = self.icon_view.connect(
            "button-press-event", self.on_icon_view_button_press
        )
        self._icon_view_selection_handler_id = self.icon_view.connect(
            "selected-children-changed", self._on_icon_view_selection_changed
        )
        self._icon_view_key_handler_id = self.icon_view.connect(
            "key-press-event", self.on_icon_view_key_press
        )

        self.icon_view.show_all()

        if selected_paths:
            for video_path in self._get_filtered_video_paths():
                if video_path not in selected_paths:
                    continue
                child = self._video_tiles.get(video_path)
                if child is not None:
                    self.icon_view.select_child(child)
        elif sync_with_playlist:
            self._sync_icon_view_selection_to_monitor_playlist()

        self._on_icon_view_selection_changed(self.icon_view)
        if self._is_video_tab_visible():
            self._start_video_grid_playback()
            self._schedule_video_grid_warmup()
        else:
            self._stop_video_grid_playback()

    def _get_selected_video_paths(self):
        if self.icon_view is None or not self.video_paths:
            return []
        if isinstance(self.icon_view, Gtk.FlowBox):
            selected_children = self.icon_view.get_selected_children()
            selected_paths = [
                getattr(child, "video_path", None) for child in selected_children
            ]
            selected_set = {p for p in selected_paths if isinstance(p, str)}
            return [p for p in self.video_paths if p in selected_set]
        return []

    def _reload_icon_view(self, *_):
        self.video_paths = get_video_paths()
        self.icon_view = self.builder.get_object("IconView")
        if self.icon_view is None:
            return
        self._rebuild_icon_view(sync_with_playlist=True)
        if _is_truthy_env("WALLBLAZER_AUTO_NORMALIZE"):
            self._start_video_normalizer()

    def _start_video_normalizer(self):
        if self._video_normalizer_thread is not None and self._video_normalizer_thread.is_alive():
            self._video_normalizer_pending = True
            return

        paths = list(self.video_paths or [])
        candidates = [path for path in paths if video_needs_normalization(path)]
        if not candidates:
            return

        self._video_normalizer_pending = False

        def _worker():
            path_map = {}
            changed = False
            for path in candidates:
                result = normalize_video_file(path, delete_original=True)
                if not result.get("ok") or not result.get("changed"):
                    continue
                changed = True
                old_path = result.get("input_path")
                new_path = result.get("output_path") or old_path
                if old_path and new_path and os.path.realpath(old_path) != os.path.realpath(new_path):
                    path_map[old_path] = new_path

            def _finish():
                if changed:
                    if path_map:
                        self._replace_video_paths_in_config(path_map=path_map)
                    elif self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
                        self._server_call_async("reload")
                    self.video_paths = get_video_paths()
                    if self.icon_view is not None:
                        self._rebuild_icon_view(sync_with_playlist=True)
                thread = self._video_normalizer_thread
                self._video_normalizer_thread = None
                if self._video_normalizer_pending and thread is not None:
                    self._video_normalizer_pending = False
                    self._start_video_normalizer()
                return False

            GLib.idle_add(_finish)

        self._video_normalizer_thread = threading.Thread(
            target=_worker,
            name="wb-video-normalizer",
            daemon=True,
        )
        self._video_normalizer_thread.start()

    def on_video_search_changed(self, entry):
        self._video_search_query = (entry.get_text() or "").strip().lower()
        selected_before = set(self._get_selected_video_paths())
        self._rebuild_icon_view(selected_paths=selected_before, sync_with_playlist=False)

    def on_file_manager_mode_toggled(self, btn):
        self._file_manager_mode = bool(btn.get_active())
        self.config["file_manager_mode"] = self._file_manager_mode
        self._save_config_delay()
        self._update_context_menu_state()

    @staticmethod
    def _human_size(size_bytes):
        size = float(max(0, int(size_bytes)))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{int(size_bytes)} B"

    @staticmethod
    def _build_non_conflicting_path(dest_dir, filename):
        candidate = os.path.join(dest_dir, filename)
        if not os.path.exists(candidate):
            return candidate

        stem, ext = os.path.splitext(filename)
        index = 1
        while True:
            suffix = "copy" if index == 1 else f"copy {index}"
            candidate = os.path.join(dest_dir, f"{stem} ({suffix}){ext}")
            if not os.path.exists(candidate):
                return candidate
            index += 1

    def _launch_detached(self, command):
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            self._show_error(f"Failed to launch command:\n{' '.join(command)}\n\n{e}")
            return False

    def _replace_video_paths_in_config(self, path_map=None, removed_paths=None):
        path_map = path_map or {}
        removed_paths = removed_paths or []
        path_map_real = {
            os.path.realpath(old): os.path.realpath(new)
            for old, new in path_map.items()
            if isinstance(old, str) and isinstance(new, str)
        }
        removed_real = {
            os.path.realpath(path)
            for path in removed_paths
            if isinstance(path, str)
        }
        changed = False

        data_source = self.config.get(CONFIG_KEY_DATA_SOURCE, {})
        if isinstance(data_source, dict):
            for monitor_name, current_path in list(data_source.items()):
                if not isinstance(current_path, str) or not current_path:
                    continue
                current_real = os.path.realpath(current_path)
                if current_real in removed_real:
                    data_source[monitor_name] = ""
                    changed = True
                elif current_real in path_map_real:
                    data_source[monitor_name] = path_map_real[current_real]
                    changed = True

        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if isinstance(library, dict):
            for playlist_name, items in list(library.items()):
                if not isinstance(items, list):
                    continue
                next_items = []
                seen = set()
                for item in items:
                    if not isinstance(item, str):
                        continue
                    item_real = os.path.realpath(item)
                    if item_real in removed_real:
                        changed = True
                        continue
                    mapped = path_map_real.get(item_real, item)
                    if mapped in seen:
                        changed = True
                        continue
                    seen.add(mapped)
                    if mapped != item:
                        changed = True
                    next_items.append(mapped)
                if next_items != items:
                    library[playlist_name] = next_items
                    changed = True

        reverse_items = self.config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if isinstance(reverse_items, dict):
            for playlist_name, items in list(reverse_items.items()):
                if not isinstance(items, list):
                    continue
                next_items = []
                seen = set()
                for item in items:
                    if not isinstance(item, str):
                        continue
                    item_real = os.path.realpath(item)
                    if item_real in removed_real:
                        changed = True
                        continue
                    mapped = path_map_real.get(item_real, item)
                    if mapped in seen:
                        changed = True
                        continue
                    seen.add(mapped)
                    if mapped != item:
                        changed = True
                    next_items.append(mapped)
                if next_items != items:
                    reverse_items[playlist_name] = next_items
                    changed = True
            self.config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = reverse_items

        if changed:
            self._sync_monitor_playlists_from_library()
            self._save_config()
            if self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
                self._server_call_async("reload")
        return changed

    def _get_video_probe_data(self, video_path):
        width = None
        height = None
        duration = None
        try:
            dim = subprocess.check_output([
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                video_path,
            ], shell=False, encoding="UTF-8").strip()
            width_str, height_str = dim.split("x")
            width, height = int(width_str), int(height_str)
        except Exception:
            pass
        try:
            duration = float(subprocess.check_output([
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ], shell=False, encoding="UTF-8").strip())
        except Exception:
            duration = None
        return width, height, duration

    def _set_clipboard_paths(self, mode, paths):
        normalized = []
        for path in paths:
            if isinstance(path, str) and os.path.isfile(path):
                normalized.append(os.path.realpath(path))
        self._fm_clipboard_mode = mode if normalized else None
        self._fm_clipboard_paths = normalized
        self._update_context_menu_state()

    def _remove_paths_from_clipboard(self, removed_paths):
        removed_real = {os.path.realpath(path) for path in removed_paths if isinstance(path, str)}
        if not removed_real:
            return
        self._fm_clipboard_paths = [
            path for path in self._fm_clipboard_paths
            if os.path.realpath(path) not in removed_real
        ]
        if not self._fm_clipboard_paths:
            self._fm_clipboard_mode = None
        self._update_context_menu_state()

    def _selected_or_error(self):
        selected = self._get_selected_video_paths()
        if not selected:
            self._show_error("No video selected.")
            return []
        return selected

    def on_fm_open_selected(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return
        try:
            open_path_default(selected[0])
        except Exception as e:
            self._show_error(f"Failed to open file:\n{e}")

    def on_fm_play_selected(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return
        if shutil.which("vlc"):
            self._launch_detached(["vlc", "--play-and-exit", selected[0]])
        else:
            try:
                open_path_default(selected[0])
            except Exception as e:
                self._show_error(f"Failed to open file:\n{e}")

    def on_fm_open_containing_folder(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return
        try:
            open_path_default(os.path.dirname(selected[0]))
        except Exception as e:
            self._show_error(f"Failed to open folder:\n{e}")

    def on_fm_copy_selected(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return
        self._set_clipboard_paths("copy", selected)
        logger.info(f"[FileManager] Copied {len(selected)} file(s)")

    def on_fm_cut_selected(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return
        self._set_clipboard_paths("cut", selected)
        logger.info(f"[FileManager] Cut {len(selected)} file(s)")

    def on_fm_paste_selected(self, *_):
        if not self._fm_clipboard_paths or self._fm_clipboard_mode not in {"copy", "cut"}:
            self._show_error("Clipboard is empty. Use Copy or Cut first.")
            return

        target_dir = os.path.realpath(VIDEO_WALLPAPER_DIR)
        os.makedirs(target_dir, exist_ok=True)
        mode = self._fm_clipboard_mode

        pasted = []
        moved_map = {}
        errors = []
        for src in list(self._fm_clipboard_paths):
            if not os.path.isfile(src):
                errors.append(f"Missing source: {src}")
                continue
            dst = self._build_non_conflicting_path(target_dir, os.path.basename(src))
            if os.path.realpath(src) == os.path.realpath(dst):
                continue
            try:
                if mode == "copy":
                    shutil.copy2(src, dst)
                else:
                    shutil.move(src, dst)
                    moved_map[src] = dst
                pasted.append(dst)
            except Exception as e:
                errors.append(f"{src}: {e}")

        if mode == "cut":
            self._fm_clipboard_mode = None
            self._fm_clipboard_paths = []

        if moved_map:
            self._replace_video_paths_in_config(path_map=moved_map)
        self._reload_icon_view()
        self._update_context_menu_state()

        if errors:
            self._show_error("Paste finished with some errors:\n" + "\n".join(errors[:8]))
        elif pasted:
            logger.info(f"[FileManager] Pasted {len(pasted)} file(s)")

    def on_fm_rename_selected(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return
        if len(selected) != 1:
            self._show_error("Rename requires exactly one selected video.")
            return

        old_path = selected[0]
        old_name = os.path.basename(old_path)
        dialog = Gtk.Dialog(
            title="Rename Video",
            transient_for=self.window,
            modal=True,
            destroy_with_parent=True,
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "Rename", Gtk.ResponseType.OK,
        )
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.add(Gtk.Label(label="New file name:"))
        entry = Gtk.Entry()
        entry.set_text(old_name)
        entry.set_activates_default(True)
        content.add(entry)
        dialog.show_all()
        response = dialog.run()
        new_name = entry.get_text().strip()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return
        if not new_name:
            self._show_error("File name cannot be empty.")
            return
        if "/" in new_name or ("\\" in new_name):
            self._show_error("File name cannot include path separators.")
            return

        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.realpath(new_path) == os.path.realpath(old_path):
            return
        if os.path.exists(new_path):
            self._show_error("A file with that name already exists.")
            return
        try:
            os.rename(old_path, new_path)
        except Exception as e:
            self._show_error(f"Failed to rename file:\n{e}")
            return

        self._replace_video_paths_in_config(path_map={old_path: new_path})
        self._remove_paths_from_clipboard([old_path])
        self._reload_icon_view()

    def on_fm_delete_selected(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return
        count = len(selected)
        dialog = Gtk.MessageDialog(
            parent=self.window,
            modal=True,
            destroy_with_parent=True,
            text=f"Delete {count} selected video(s)?",
            message_type=Gtk.MessageType.QUESTION,
            secondary_text="Files will be moved to trash when possible.",
            buttons=Gtk.ButtonsType.NONE,
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "Delete", Gtk.ResponseType.OK,
        )
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.OK:
            return

        deleted = []
        errors = []
        for path in selected:
            try:
                gio_file = Gio.File.new_for_path(path)
                try:
                    gio_file.trash(None)
                except Exception:
                    os.remove(path)
                deleted.append(path)
            except Exception as e:
                errors.append(f"{path}: {e}")

        if deleted:
            self._replace_video_paths_in_config(removed_paths=deleted)
            self._remove_paths_from_clipboard(deleted)
            self._reload_icon_view()
        if errors:
            self._show_error("Delete finished with some errors:\n" + "\n".join(errors[:8]))

    def on_fm_properties_selected(self, *_):
        selected = self._selected_or_error()
        if not selected:
            return

        if len(selected) > 1:
            total_bytes = 0
            for path in selected:
                try:
                    total_bytes += os.path.getsize(path)
                except OSError:
                    pass
            secondary = (
                f"Selected files: {len(selected)}\n"
                f"Total size: {self._human_size(total_bytes)}\n"
                f"Folder: {VIDEO_WALLPAPER_DIR}"
            )
            dialog = Gtk.MessageDialog(
                parent=self.window,
                modal=True,
                destroy_with_parent=True,
                text="Properties",
                message_type=Gtk.MessageType.INFO,
                secondary_text=secondary,
                buttons=Gtk.ButtonsType.OK,
            )
            dialog.run()
            dialog.destroy()
            return

        path = selected[0]
        try:
            stat = os.stat(path)
        except OSError as e:
            self._show_error(f"Unable to read file properties:\n{e}")
            return

        mime_type = mimetypes.guess_type(path)[0] or "unknown"
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        width, height, duration = self._get_video_probe_data(path)
        video_line = "Video: unknown"
        if width and height:
            video_line = f"Video: {width}x{height}"
        if duration is not None:
            mins = int(duration // 60)
            secs = int(duration % 60)
            video_line += f" | Duration: {mins:02d}:{secs:02d}"

        secondary = (
            f"Name: {os.path.basename(path)}\n"
            f"Path: {path}\n"
            f"Type: {mime_type}\n"
            f"Size: {self._human_size(stat.st_size)}\n"
            f"Modified: {modified}\n"
            f"{video_line}"
        )
        dialog = Gtk.MessageDialog(
            parent=self.window,
            modal=True,
            destroy_with_parent=True,
            text="Properties",
            message_type=Gtk.MessageType.INFO,
            secondary_text=secondary,
            buttons=Gtk.ButtonsType.OK,
        )
        dialog.run()
        dialog.destroy()

    def _on_icon_view_selection_changed(self, icon_view):
        selected = self._get_selected_video_paths()
        lbl: Gtk.Label = self.builder.get_object("LabelSelectionInfo")
        count = len(selected)
        visible_count = len(icon_view.get_children()) if isinstance(icon_view, Gtk.FlowBox) else 0
        total_count = len(self.video_paths) if self.video_paths else 0
        if count == 0:
            if lbl is not None:
                if self._video_search_query and visible_count == 0:
                    lbl.set_text(f'No videos match "{self._video_search_query}"')
                elif total_count == 0:
                    lbl.set_text(
                        f"No videos found — add MP4/WebM files to:\n{VIDEO_WALLPAPER_DIR}"
                    )
                else:
                    lbl.set_text("Hold Ctrl/Shift to select multiple videos")
        elif count == 1:
            if lbl is not None:
                lbl.set_text(f"1 video selected: {os.path.basename(selected[0])}")
        else:
            if lbl is not None:
                lbl.set_text(f"{count} videos selected — click 'Apply to Monitor' or '+ Playlist'")
        self._update_context_menu_state()

    def on_icon_view_key_press(self, _widget, event):
        if not self._file_manager_mode:
            return False
        key_name = (Gdk.keyval_name(event.keyval) or "").lower()
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)

        if ctrl and key_name == "c":
            self.on_fm_copy_selected()
            return True
        if ctrl and key_name == "x":
            self.on_fm_cut_selected()
            return True
        if ctrl and key_name == "v":
            self.on_fm_paste_selected()
            return True
        if key_name == "delete":
            self.on_fm_delete_selected()
            return True
        if key_name == "f2":
            self.on_fm_rename_selected()
            return True
        if key_name in {"return", "kp_enter"}:
            self.on_fm_play_selected()
            return True
        return False

    def on_icon_view_button_press(self, widget, event):
        if event.button == Gdk.BUTTON_SECONDARY:  # Right click
            if isinstance(widget, Gtk.FlowBox):
                self.icon_view.grab_focus()
                self._update_context_menu_state()
                self.contextMenu_monitors.show_all()
                self.contextMenu_monitors.popup(
                    None, None, None, None, 0, Gtk.get_current_event_time())
                return True
        return False

    def on_icon_tile_button_press(self, _widget, event, tile):
        if event.button != Gdk.BUTTON_SECONDARY or self.icon_view is None:
            return False
        self.icon_view.grab_focus()
        selected_children = self.icon_view.get_selected_children()
        if tile not in selected_children:
            self.icon_view.unselect_all()
            self.icon_view.select_child(tile)
        self._update_context_menu_state()
        self.contextMenu_monitors.show_all()
        self.contextMenu_monitors.popup(
            None, None, None, None, 0, Gtk.get_current_event_time()
        )
        return True

    # ──────────────────────────────────────────────────
    #  Apply video to monitor
    # ──────────────────────────────────────────────────
    def on_local_video_apply(self, *_):
        selected = self._get_selected_video_paths()
        if not selected:
            self._show_error("No video selected.\nPlease choose at least one.")
            return
        if len(selected) > 1:
            # Multiple selected → offer to add to playlist
            dialog = Gtk.MessageDialog(
                parent=self.window, modal=True, destroy_with_parent=True,
                text="Multiple Videos Selected",
                message_type=Gtk.MessageType.QUESTION,
                secondary_text=(
                    "You selected multiple videos.\n"
                    "Click 'Add to Playlist' to queue them as a playlist,\n"
                    "or 'Apply First' to apply only the first video."
                ),
                buttons=Gtk.ButtonsType.NONE,
            )
            dialog.add_buttons(
                "Apply First", Gtk.ResponseType.NO,
                "Add to Playlist", Gtk.ResponseType.YES,
                "Cancel", Gtk.ResponseType.CANCEL,
            )
            resp = dialog.run()
            dialog.destroy()
            if resp == Gtk.ResponseType.YES:
                self.on_add_to_playlist_clicked()
                return
            elif resp == Gtk.ResponseType.NO:
                selected = [selected[0]]
            else:
                return
        # Single video: apply to all monitors
        self.on_set_as(None, self.all_key)

    def on_set_as(self, widget, monitor):
        selected = self._get_selected_video_paths()
        if not selected:
            self._show_error("No video selected.")
            return
        video_path = selected[0]
        logger.info("[GUI] Applied selected video to monitor target.")
        self.config[CONFIG_KEY_MODE] = MODE_VIDEO
        paths = self.config.get(CONFIG_KEY_DATA_SOURCE) or {}
        target_monitor_name = None
        if monitor == self.all_key:
            for name, mon_obj in self.monitors.get_monitors().items():
                paths[name] = video_path
                mon_obj.set_wallpaper(video_path)
        else:
            target_monitor_name = monitor.name
            paths[target_monitor_name] = video_path
            self.monitors.get_monitor(target_monitor_name).set_wallpaper(video_path)
        paths["Default"] = video_path
        self.config[CONFIG_KEY_DATA_SOURCE] = paths
        self._save_config()
        if self.server is not None:
            if monitor == self.all_key:
                self._server_call_async("reload")
            else:
                self._server_call_async("video", video_path, target_monitor_name)

    def on_local_web_page_apply(self, *_):
        file_chooser: Gtk.FileChooserButton = self.builder.get_object("FileChooser")
        if file_chooser is None:
            return
        chosen = file_chooser.get_file()
        if chosen is None:
            self._show_error("Please choose a HTML file")
            return
        file_path = chosen.get_path()
        self.config[CONFIG_KEY_MODE] = MODE_WEBPAGE
        self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = file_path
        self._save_config()
        self._server_call_async("webpage", file_path)

    # ──────────────────────────────────────────────────
    #  Toggle handlers (wired in Python, not XML)
    # ──────────────────────────────────────────────────
    def on_autostart_toggled(self, btn):
        self.is_autostart = btn.get_active()
        setup_autostart(self.is_autostart)

    def on_static_wallpaper_toggled(self, btn):
        self.config[CONFIG_KEY_STATIC_WALLPAPER] = btn.get_active()
        self._save_config()
        self._server_set_async("is_static_wallpaper", self.config[CONFIG_KEY_STATIC_WALLPAPER])
        self.set_spin_blur_radius_sensitive()

    def on_pause_when_maximized_toggled(self, btn):
        self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED] = btn.get_active()
        self._save_config()
        self._server_set_async("is_pause_when_maximized", self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED])

    def on_mute_when_maximized_toggled(self, btn):
        self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED] = btn.get_active()
        self._save_config()
        self._server_set_async("is_mute_when_maximized", self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED])

    def on_playlist_toggled(self, btn):
        enabled = btn.get_active()
        self.config[CONFIG_KEY_PLAYLIST] = enabled
        self._save_config()
        self._server_call_async("reload")
        # Show/hide playlist controls
        self._set_visible("PlaylistControlRow", True)

    def on_playlist_shuffle_toggled(self, btn):
        self.config[CONFIG_KEY_PLAYLIST_SHUFFLE] = btn.get_active()
        self._save_config()
        self._server_call_async("reload")

    def on_reverse_single_toggled(self, btn):
        self.config[CONFIG_KEY_REVERSE_SINGLE] = btn.get_active()
        self._save_config()
        if self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
            self._server_call_async("reload")

    def on_playlist_reverse_all_toggled(self, btn):
        self.config[CONFIG_KEY_REVERSE_PLAYLIST] = btn.get_active()
        self._save_config()
        if self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
            self._server_call_async("reload")

    def on_mute_toggled(self, btn):
        self.config[CONFIG_KEY_MUTE] = btn.get_active()
        self._save_config()
        self._server_set_async("is_mute", self.config[CONFIG_KEY_MUTE])
        self.set_mute_toggle_icon()
        self.set_scale_volume_sensitive()

    def on_playlist_interval_changed(self, spin):
        minutes = max(0, int(spin.get_value()))
        # 0 minutes means "advance when video ends" → interval_sec = 0
        seconds = 0 if minutes == 0 else int(minutes * 60)
        self.config[CONFIG_KEY_PLAYLIST_INTERVAL] = seconds
        self._save_config_delay()
        self._server_call_async("reload")

    def on_theme_changed(self, combo):
        theme_list = ["system", "dark", "light"]
        idx = combo.get_active()
        theme = theme_list[idx] if 0 <= idx < len(theme_list) else "system"
        self.config[CONFIG_KEY_THEME] = theme
        self._save_config()
        apply_gtk_theme(theme)
        self._apply_theme_css(theme)

    # ──────────────────────────────────────────────────
    #  Volume / Blur / Mute icon
    # ──────────────────────────────────────────────────
    def on_volume_changed(self, adjustment):
        self.config[CONFIG_KEY_VOLUME] = int(adjustment.get_value())
        self._save_config_delay()
        self._server_set_async("volume", self.config[CONFIG_KEY_VOLUME])
        self.set_mute_toggle_icon()

    def on_blur_radius_changed(self, adjustment):
        self.config[CONFIG_KEY_BLUR_RADIUS] = int(adjustment.get_value())
        self._save_config_delay()
        self._server_set_async("blur_radius", self.config[CONFIG_KEY_BLUR_RADIUS])

    def set_mute_toggle_icon(self):
        icon_widget: Gtk.Image = self.builder.get_object("ToggleMuteIcon")
        if icon_widget is None:
            return
        volume = self.config.get(CONFIG_KEY_VOLUME, 50)
        is_mute = self.config.get(CONFIG_KEY_MUTE, False)
        if volume == 0 or is_mute:
            icon_name = "audio-volume-muted-symbolic"
        elif volume < 30:
            icon_name = "audio-volume-low-symbolic"
        elif volume < 60:
            icon_name = "audio-volume-medium-symbolic"
        else:
            icon_name = "audio-volume-high-symbolic"
        icon_widget.set_from_icon_name(icon_name, 0)

    def set_scale_volume_sensitive(self):
        scale = self.builder.get_object("ScaleVolume")
        if scale:
            scale.set_sensitive(not self.config.get(CONFIG_KEY_MUTE, False))

    def set_spin_blur_radius_sensitive(self):
        spin = self.builder.get_object("SpinBlurRadius")
        if spin:
            spin.set_sensitive(self.config.get(CONFIG_KEY_STATIC_WALLPAPER, True))

    # ──────────────────────────────────────────────────
    #  Other actions
    # ──────────────────────────────────────────────────
    def on_play_pause(self, *_):
        def _toggle():
            try:
                server = get_service(DBUS_NAME_SERVER)
                if server is None:
                    raise RuntimeError("server unavailable")
                # Bug 3: the property setter already communicates pause state to the player.
                # Calling pause_playback()/start_playback() on top was redundant and racy.
                prev = server.is_paused_by_user
                server.is_paused_by_user = not prev
            except Exception as e:
                logger.warning(f"[GUI] play_pause error: {e}")
        threading.Thread(target=_toggle, daemon=True).start()

    def on_feeling_lucky(self, *_):
        self._server_call_async("feeling_lucky")

    def on_open_config(self, *_):
        open_path_default(os.path.realpath(CONFIG_PATH))

    def on_about(self, *_):
        about: Gtk.AboutDialog = self.builder.get_object("AboutDialog")
        if about is None:
            return
        about.set_transient_for(self.window)
        about.set_version(self.version)
        about.set_modal(True)
        about.present()

    def on_streaming_activate(self, entry: Gtk.Entry, *_):
        url = entry.get_text().strip()
        if not url:
            return

        def _check_url():
            try:
                with yt_dlp.YoutubeDL({"noplaylist": True, "quiet": True}) as ydl:
                    ydl.extract_info(url, download=False)
                GLib.idle_add(_on_success)
            except Exception as e:
                s = " ".join(str(e).split(" ")[1:])
                GLib.idle_add(self._show_error, f"Failed to stream:\n{s}")

        def _on_success():
            self.config[CONFIG_KEY_MODE] = MODE_STREAM
            self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = url
            self._save_config()
            self._server_call_async("stream", url)

        threading.Thread(target=_check_url, daemon=True).start()

    def on_web_page_activate(self, entry: Gtk.Entry, *_):
        url = entry.get_text().strip()
        if not url:
            return

        def _check_url():
            try:
                r = requests.get(url, timeout=5)
                if r.status_code >= 400:
                    GLib.idle_add(self._show_error, f"Failed to access {url} (HTTP {r.status_code})")
                    return
                GLib.idle_add(_on_success)
            except Exception as e:
                GLib.idle_add(self._show_error, f"Failed to access {url}:\n{e}")

        def _on_success():
            self.config[CONFIG_KEY_MODE] = MODE_WEBPAGE
            self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = url
            self._save_config()
            self._server_call_async("webpage", url)

        threading.Thread(target=_check_url, daemon=True).start()

    def on_quit(self, *_):
        if self._gpu_status_timer_id is not None:
            try:
                GLib.source_remove(self._gpu_status_timer_id)
            except Exception:
                pass
            self._gpu_status_timer_id = None
        self._release_video_grid_instance()
        if self.server is not None:
            try:
                self.server.quit()
            except GLib.Error:
                pass
        self.quit()

    def do_shutdown(self):
        if self._gpu_status_timer_id is not None:
            try:
                GLib.source_remove(self._gpu_status_timer_id)
            except Exception:
                pass
            self._gpu_status_timer_id = None
        self._release_video_grid_instance()
        Gtk.Application.do_shutdown(self)

    # ──────────────────────────────────────────────────
    #  Dialogs
    # ──────────────────────────────────────────────────
    def _show_welcome(self):
        dialog = Gtk.MessageDialog(
            parent=self.window, modal=True, destroy_with_parent=True,
            text="Welcome to Wall Blazer 🤗",
            message_type=Gtk.MessageType.INFO,
            secondary_text=(
                "Quickstart:\n"
                f"  • Add your MP4/WebM videos to:\n"
                f"    {VIDEO_WALLPAPER_DIR}\n"
                "  • Click the folder icon (or use the refresh button)\n"
                "  • Select a video and click 'Apply' to use it as wallpaper\n"
                "  • Multi-select videos (Ctrl/Shift), then click '+ Playlist'"
            ),
            buttons=Gtk.ButtonsType.OK,
        )
        dialog.run()
        dialog.destroy()

    def _show_error(self, error):
        dialog = Gtk.MessageDialog(
            parent=self.window, modal=True, destroy_with_parent=True,
            text="Oops!",
            message_type=Gtk.MessageType.ERROR,
            secondary_text=error,
            buttons=Gtk.ButtonsType.OK,
        )
        dialog.run()
        dialog.destroy()


def main(version="devel", pkgdatadir="/app/share/wallblazer", localedir="/app/share/locale"):
    os.environ["WALLBLAZER_PKGDATADIR"] = str(pkgdatadir)
    try:
        resource = Gio.Resource.load(os.path.join(pkgdatadir, "wallblazer.gresource"))
        resource._register()
        icon_theme = Gtk.IconTheme.get_default()
        if icon_theme is not None:
            icon_theme.add_resource_path("/io/wallblazer/WallBlazer/icons")
    except GLib.Error:
        logger.warning("[GUI] Couldn't load resource; falling back to direct UI files")

    app = ControlPanel(version)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
