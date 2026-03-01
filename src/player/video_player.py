import sys
import glob
import time
import random
import ctypes
import logging
import pathlib
import math
import subprocess
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, Gdk, GLib

import vlc
from pydbus import SessionBus
from PIL import Image, ImageFilter

try:
    import os
    sys.path.insert(1, os.path.join(sys.path[0], '..'))
    from player.base_player import BasePlayer
    from menu import build_menu
    from commons import *
    from utils import ActiveHandler, ConfigUtil, is_gnome, is_wayland, is_nvidia_proprietary, is_vdpau_ok, is_flatpak, get_vlc_hwdec_profile
    from yt_utils import get_formats, get_best_audio, get_optimal_video
except (ModuleNotFoundError, ImportError):
    from wallblazer.player.base_player import BasePlayer
    from wallblazer.menu import build_menu
    from wallblazer.commons import *
    from wallblazer.utils import ActiveHandler, ConfigUtil, is_gnome, is_wayland, is_nvidia_proprietary, is_vdpau_ok, is_flatpak, get_vlc_hwdec_profile
    from wallblazer.yt_utils import get_formats, get_best_audio, get_optimal_video

logger = logging.getLogger(LOGGER_NAME)

INSTANT_PLAYLIST_SWITCH_LEAD_MS = 500
INSTANT_PLAYLIST_POLL_INTERVAL_MS = 100

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_LOW_END_MODE_ENV = "WALLBLAZER_LOW_END_MODE"


def _is_truthy_env(var_name):
    value = str(os.environ.get(var_name, "")).strip().lower()
    return value in _TRUTHY_ENV_VALUES


def _build_vlc_options():
    # Shared options.
    options = [
        "--no-disable-screensaver",
        "--no-video-title-show",
        "--no-osd",
        "--no-spu",
        "--no-stats",
        "--avcodec-threads=0",
    ]

    if _is_truthy_env(_LOW_END_MODE_ENV):
        # Optional fallback profile for very low-end systems.
        logger.warning(
            f"[VLC] {_LOW_END_MODE_ENV}=1: enabling aggressive low-end decode profile"
        )
        options.extend([
            "--drop-late-frames",
            "--skip-frames",
            "--avcodec-fast",
            "--clock-jitter=0",
            "--clock-synchro=0",
            "--avcodec-skiploopfilter=4",
            "--swscale-mode=0",
            "--avcodec-skip-frame=2",
            "--avcodec-skip-idct=2",
            "--network-caching=3000",
        ])
    else:
        # Default profile prioritizes image quality and smooth playback.
        options.extend([
            "--drop-late-frames",
            "--avcodec-skiploopfilter=0",
            "--avcodec-skip-frame=0",
            "--avcodec-skip-idct=0",
            "--swscale-mode=2",
            "--network-caching=3000",
        ])

    return options


if is_wayland():
    # TODO: Window event monitoring for GNOME Wayland is broken
    class WindowHandler:
        def __init__(self, _: callable):
            pass
else:
    try:
        from utils import WindowHandler
    except (ModuleNotFoundError, ImportError):
        from wallblazer.utils import WindowHandler


class Fade:
    def __init__(self):
        self.timer = None
        self.is_active = False

    def start(self, cur, target, step, fade_interval, update_callback: callable = None,
              complete_callback: callable = None):
        # Cancel any existing timer first
        self.cancel()
        self.is_active = True
        self._fade_step(cur, target, step, fade_interval, update_callback, complete_callback)

    def _fade_step(self, cur, target, step, fade_interval, update_callback, complete_callback):
        if not self.is_active:
            return
            
        new_cur = cur + step
        if (step < 0 and new_cur <= target) or (step > 0 and new_cur >= target):
            new_cur = target
            if update_callback:
                update_callback(int(new_cur))
            if complete_callback:
                complete_callback()
            self.is_active = False
        else:
            if update_callback:
                update_callback(int(new_cur))
            self.timer = threading.Timer(
                fade_interval,
                self._fade_step,
                args=[new_cur, target, step, fade_interval, update_callback, complete_callback],
            )
            self.timer.daemon = True  # Make timer daemon to prevent blocking shutdown
            self.timer.start()

    def cancel(self):
        self.is_active = False
        if self.timer:
            self.timer.cancel()
            self.timer = None


class VLCWidget(Gtk.DrawingArea):
    """
    Simple VLC widget.
    Its player can be controlled through the 'player' attribute, which
    is a vlc.MediaPlayer() instance.
    """
    __gtype_name__ = "VLCWidget"

    def __init__(self, width, height):
        Gtk.DrawingArea.__init__(self)

        # Spawn a VLC instance and create a new media player to embed.
        # Some options need to be specified when instantiating VLC.
        vlc_options = _build_vlc_options()
        gpu_profile = get_vlc_hwdec_profile()
        vlc_options.append(f"--avcodec-hw={gpu_profile['hwdec']}")
        logger.info(
            f"[VLC] hwdec={gpu_profile['hwdec']} "
            f"gpu_available={gpu_profile['gpu_available']} reason={gpu_profile['reason']}"
        )
        self.instance = vlc.Instance(vlc_options)
        self.player = self.instance.media_player_new()

        def handle_embed(*args):
            if sys.platform == "win32":
                import ctypes
                
                # Send 0x052C to Progman to spawn a WorkerW behind the desktop icons
                progman = ctypes.windll.user32.FindWindowW("Progman", None)
                ctypes.windll.user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, None)

                workerw = [0]

                def enum_windows(hwnd, lParam):
                    shell_dll_defer_view = ctypes.windll.user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
                    if shell_dll_defer_view:
                        workerw[0] = ctypes.windll.user32.FindWindowExW(0, hwnd, "WorkerW", None)
                    return True

                EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_windows), 0)

                if workerw[0]:
                    self.player.set_hwnd(workerw[0])
            else:
                self.player.set_xwindow(self.get_window().get_xid())
            return True

        # Embed and set size.
        self.connect("realize", handle_embed)
        self.set_size_request(width, height)

    def cleanup(self):
        """Cleanup VLC resources to prevent memory leaks"""
        try:
            if self.player:
                self.player.stop()
                self.player.release()
                self.player = None
            if self.instance:
                self.instance.release()
                self.instance = None
        except Exception as e:
            logger.warning(f"[VLCWidget] Cleanup error: {e}")


class PlayerWindow(Gtk.ApplicationWindow):
    def __init__(self, name, width, height, *args, **kwargs):
        super(PlayerWindow, self).__init__(*args, **kwargs)
        # Setup a VLC widget given the provided width and height.
        self.width = width
        self.height = height
        self.name = name
        self.__vlc_widget = VLCWidget(width, height)
        self.add(self.__vlc_widget)
        self.__vlc_widget.show()

        # These are to allow us to right click. VLC can't hijack mouse input, and probably not key inputs either in
        # Case we want to add keyboard shortcuts later on.
        self.__vlc_widget.player.video_set_mouse_input(False)
        self.__vlc_widget.player.video_set_key_input(False)

        # A timer that handling fade-in/out
        self.fade = Fade()
        self._queued_media = None
        self._queued_source = None
        self._queued_dimensions = (None, None)

        self.menu = None
        self.connect("button-press-event", self._on_button_press_event)

    def play(self):
        self.__vlc_widget.player.play()

    def play_fade(self, target, fade_duration_sec, fade_interval):
        self.play()
        cur = 0
        step = (target - cur) / (fade_duration_sec / fade_interval)
        self.fade.cancel()
        self.fade.start(cur=cur, target=target, step=step,
                        fade_interval=fade_interval, update_callback=self.set_volume)

    def is_playing(self):
        return self.__vlc_widget.player.is_playing()

    def pause(self):
        if self.is_playing():
            self.__vlc_widget.player.pause()

    def pause_fade(self, fade_duration_sec, fade_interval):
        cur = self.get_volume()
        target = 0
        step = (target - cur) / (fade_duration_sec / fade_interval)
        self.fade.cancel()
        self.fade.start(cur=cur, target=target, step=step, fade_interval=fade_interval, update_callback=self.set_volume,
                        complete_callback=self.pause)

    def volume_fade(self, target, fade_duration_sec, fade_interval):
        cur = self.get_volume()
        step = (target - cur) / (fade_duration_sec / fade_interval)
        self.fade.cancel()
        self.fade.start(cur=cur, target=target, step=step, fade_interval=fade_interval, update_callback=self.set_volume)

    def media_new(self, *args):
        return self.__vlc_widget.instance.media_new(*args)

    def set_media(self, *args):
        self.__vlc_widget.player.set_media(*args)

    def queue_media(self, media, source, video_width=None, video_height=None):
        self._queued_media = media
        self._queued_source = source
        self._queued_dimensions = (video_width, video_height)

    def queued_source(self):
        return self._queued_source

    def clear_queued_media(self):
        self._queued_media = None
        self._queued_source = None
        self._queued_dimensions = (None, None)

    def switch_to_queued_media(self, should_play=True):
        if not self._queued_media:
            return None
        media = self._queued_media
        source = self._queued_source
        video_width, video_height = self._queued_dimensions
        self.clear_queued_media()
        self.set_media(media)
        self.set_position(0.0)
        self.centercrop(video_width, video_height)
        if should_play:
            self.play()
        return source

    def set_volume(self, *args):
        self.__vlc_widget.player.audio_set_volume(*args)

    def get_volume(self):
        return self.__vlc_widget.player.audio_get_volume()

    def set_mute(self, is_mute):
        return self.__vlc_widget.player.audio_set_mute(is_mute)

    def get_position(self):
        return self.__vlc_widget.player.get_position()

    def set_position(self, *args):
        self.__vlc_widget.player.set_position(*args)

    def get_time(self):
        return self.__vlc_widget.player.get_time()

    def get_length(self):
        return self.__vlc_widget.player.get_length()

    def snapshot(self, *args):
        return self.__vlc_widget.player.video_take_snapshot(*args)

    def centercrop(self, video_width=None, video_height=None):
        # Getting dimension from libvlc is not reliable enough (need to consider timing)
        if (video_width, video_height) == (None, None):
            video_width, video_height = self.__vlc_widget.player.video_get_size()
            if video_width == 0 or video_height == 0:
                logger.warning("[CenterCrop] video_get_size is not ready yet")
                return
        logger.debug(f"[CenterCrop] Dimension {video_width}x{video_height}")

        # Keep VLC in autoscale mode so output always fits monitor/window size.
        self.__vlc_widget.player.video_set_scale(0)

        window_ratio = self.width / self.height
        video_ratio = video_width / video_height
        if abs(window_ratio - video_ratio) <= 1e-3:
            # Clear any previous crop when the current source already matches.
            try:
                self.__vlc_widget.player.video_set_crop_geometry(None)
            except TypeError:
                self.__vlc_widget.player.video_set_crop_geometry("")
            return

        # Ratio-based crop is the most reliable cross-platform zoom-to-fill mode in VLC.
        target_w = max(1, int(round(self.width)))
        target_h = max(1, int(round(self.height)))
        divisor = math.gcd(target_w, target_h) or 1
        crop_ratio = f"{target_w // divisor}:{target_h // divisor}"
        logger.debug(f"[CenterCrop] Crop ratio: {crop_ratio}")
        self.__vlc_widget.player.video_set_crop_geometry(crop_ratio)

    def add_audio_track(self, audio):
        self.__vlc_widget.player.add_slave(vlc.MediaSlaveType(1), audio, True)

    def _on_button_press_event(self, widget, event):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            if not self.menu:
                self.menu = build_menu(MODE_VIDEO)
            self.menu.popup_at_pointer()
            return True
        return False

    def get_name(self):
        return self.name

    def cleanup(self):
        """Cleanup resources to prevent memory leaks"""
        self.fade.cancel()
        self.clear_queued_media()
        if self.__vlc_widget:
            self.__vlc_widget.cleanup()

    def media_event_manager(self):
        return self.__vlc_widget.player.event_manager()


class VideoPlayer(BasePlayer):
    """
    <node>
    <interface name='io.github.wallblazer.wallblazer.player'>
        <property name="mode" type="s" access="read"/>
        <property name="data_source" type="s" access="readwrite"/>
        <property name="volume" type="i" access="readwrite"/>
        <property name="is_mute" type="b" access="readwrite"/>
        <property name="is_playing" type="b" access="read"/>
        <property name="is_paused_by_user" type="b" access="readwrite"/>
        <method name='reload_config'/>
        <method name='apply_video_config'/>
        <method name='playlist_next'/>
        <method name='pause_playback'/>
        <method name='start_playback'/>
        <method name='quit_player'/>
    </interface>
    </node>
    """

    def __init__(self, *args, **kwargs):
        super(VideoPlayer, self).__init__(*args, **kwargs)

        # We need to initialize X11 threads so we can use hardware decoding.
        # `libX11.so.6` fix for Fedora 33
        x11 = None
        if is_wayland() and is_nvidia_proprietary() and not is_vdpau_ok():
            logger.warning(
                "Proprietary Nvidia driver detected! HW Acceleration is not yet working in Wayland.")
        else:
            for lib in ["libX11.so", "libX11.so.6"]:
                try:
                    x11 = ctypes.cdll.LoadLibrary(lib)
                except OSError:
                    pass
                if x11 is not None:
                    x11.XInitThreads()
                    break

        self.config = None
        self.reload_config()

        # Static wallpaper (currently for GNOME only)
        if is_gnome():
            self.original_wallpaper_uri = None
            self.original_wallpaper_uri_dark = None
            if is_flatpak():
                try:
                    self.original_wallpaper_uri = subprocess.check_output(
                        "flatpak-spawn --host gsettings get org.gnome.desktop.background picture-uri", shell=True, encoding='UTF-8')
                    self.original_wallpaper_uri_dark = subprocess.check_output(
                        "flatpak-spawn --host gsettings get org.gnome.desktop.background picture-uri-dark", shell=True, encoding='UTF-8')
                except subprocess.CalledProcessError as e:
                    logger.error(f"[StaticWallpaper] {e}")
            else:
                gso = Gio.Settings.new("org.gnome.desktop.background")
                self.original_wallpaper_uri = gso.get_string("picture-uri")
                self.original_wallpaper_uri_dark = gso.get_string(
                    "picture-uri-dark")

        # Handler should be created after everything initialized
        self.active_handler, self.window_handler = None, None
        self.is_any_maximized, self.is_any_fullscreen = False, False
        self.is_paused_by_user = False
        self._playlist_monitor_timers = {}
        self._playlist_switching_monitors = set()
        self._video_dimension_cache = {}

    def new_window(self, gdk_monitor):
        rect = gdk_monitor.get_geometry()
        return PlayerWindow(gdk_monitor.get_model(), rect.width, rect.height, application=self)

    def do_activate(self):
        super().do_activate()
        if self.mode in [MODE_VIDEO, MODE_STREAM]:
            self.data_source = self.config[CONFIG_KEY_DATA_SOURCE]
        else:
            logger.info(f"[Player] Skipping media activation for mode={self.mode}")

    def _on_monitor_added(self, _, gdk_monitor, *args):
        super()._on_monitor_added(_, gdk_monitor, *args)
        self.monitor_sync()

    def _on_active_changed(self, active):
        if active:
            self.pause_playback()
        else:
            if self._should_playback_start():
                self.start_playback()
            else:
                self.pause_playback()

    def _on_window_state_changed(self, state):
        self.is_any_maximized, self.is_any_fullscreen = state["is_any_maximized"], state["is_any_fullscreen"]
        logger.info(f"is_any_maximized: {self.is_any_maximized}, is_any_fullscreen: {self.is_any_fullscreen}")

        if self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED]:
            if self._should_playback_start():
                self.start_playback()
            else:
                self.pause_playback()
        elif self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED]:
            for monitor, window in self.windows.items():
                if not monitor.is_primary():
                    continue
                if self.is_any_fullscreen or self.is_any_maximized:
                    window.volume_fade(target=0, fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                                fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL])
                else:
                    window.volume_fade(target=self.volume, fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                                fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL])
        
    def _should_playback_start(self):
        if self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED] and (self.is_any_maximized or self.is_any_fullscreen):
            return False
        if self.is_paused_by_user:
            return False
        return True

    @staticmethod
    def _valid_playlist_entries(videos):
        if not isinstance(videos, list):
            return []
        return [video for video in videos if isinstance(video, str) and os.path.isfile(video)]

    def _is_instant_playlist_mode(self):
        return (
            self.mode == MODE_VIDEO
            and bool(self.config.get(CONFIG_KEY_PLAYLIST, False))
            and int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300)) == 0
        )

    def _get_source_for_monitor(self, monitor_name, data_source):
        monitor_source = data_source.get(monitor_name, "")
        if isinstance(monitor_source, str) and monitor_source:
            return monitor_source
        default_source = data_source.get("Default", "")
        if isinstance(default_source, str):
            return default_source
        return ""

    def _probe_video_dimensions(self, video_path):
        if not video_path:
            return (None, None)
        cached = self._video_dimension_cache.get(video_path)
        if cached is not None:
            return cached
        try:
            dimension = subprocess.check_output([
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
            width_str, height_str = dimension.split("x")
            parsed = (int(width_str), int(height_str))
        except (ValueError, subprocess.CalledProcessError, FileNotFoundError):
            parsed = (None, None)
        self._video_dimension_cache[video_path] = parsed
        return parsed

    def _create_video_media(self, window, source, loop_video, disable_audio):
        media = window.media_new(source)
        if loop_video:
            media.add_option("input-repeat=65535")
        media.add_option("no-video-title-show")
        media.add_option("file-caching=1500")
        if disable_audio:
            media.add_option("no-audio")
        return media

    def _next_playlist_source(self, monitor_name, current_source):
        videos = self._monitor_playlist_videos(monitor_name)
        if not videos:
            return None
        shuffle = bool(self.config.get(CONFIG_KEY_PLAYLIST_SHUFFLE, False))
        if shuffle:
            if len(videos) == 1:
                return videos[0]
            candidates = [video for video in videos if video != current_source]
            if not candidates:
                candidates = videos
            return random.choice(candidates)
        if current_source in videos:
            idx = videos.index(current_source)
            return videos[(idx + 1) % len(videos)]
        return videos[0]

    def _monitor_playlist_videos(self, monitor_name):
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        assignments = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if isinstance(library, dict) and isinstance(assignments, dict):
            playlist_name = assignments.get(monitor_name)
            if isinstance(playlist_name, str):
                return self._valid_playlist_entries(library.get(playlist_name, []))
        monitor_playlists = self.config.get(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        if isinstance(monitor_playlists, dict):
            return self._valid_playlist_entries(monitor_playlists.get(monitor_name, []))
        return []

    def _stop_playlist_timers(self):
        for timer_id in self._playlist_monitor_timers.values():
            try:
                GLib.source_remove(timer_id)
            except Exception:
                pass
        self._playlist_monitor_timers.clear()
        self._playlist_switching_monitors.clear()
        for window in self.windows.values():
            if window:
                window.clear_queued_media()

    def _prepare_next_media(self, monitor, window, current_source):
        monitor_name = monitor.get_model()
        next_source = self._next_playlist_source(monitor_name, current_source)
        if not next_source:
            window.clear_queued_media()
            return
        if window.queued_source() == next_source:
            return
        video_width, video_height = self._probe_video_dimensions(next_source)
        media = self._create_video_media(
            window=window,
            source=next_source,
            loop_video=False,
            disable_audio=not monitor.is_primary(),
        )
        window.queue_media(media, next_source, video_width, video_height)
        logger.debug(f"[Playlist] Preloaded next video for {monitor_name}: {next_source}")

    def _persist_data_source(self):
        try:
            ConfigUtil().save(self.config)
        except Exception as e:
            logger.warning(f"[Playlist] Could not persist data source: {e}")

    def _transition_to_queued_media(self, monitor, window):
        monitor_name = monitor.get_model()
        if monitor_name in self._playlist_switching_monitors:
            return
        queued_source = window.queued_source()
        if not queued_source:
            current_source = self._get_source_for_monitor(
                monitor_name, self.config.get(CONFIG_KEY_DATA_SOURCE, {})
            )
            self._prepare_next_media(monitor, window, current_source)
            queued_source = window.queued_source()
            if not queued_source:
                return

        self._playlist_switching_monitors.add(monitor_name)
        try:
            switched_source = window.switch_to_queued_media(
                should_play=self._should_playback_start()
            )
            if not switched_source:
                return
            self.config[CONFIG_KEY_DATA_SOURCE][monitor_name] = switched_source
            if (
                monitor_name == "Default"
                or not self.config[CONFIG_KEY_DATA_SOURCE].get("Default")
            ):
                self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = switched_source
            self._persist_data_source()
            self._prepare_next_media(monitor, window, switched_source)
        finally:
            self._playlist_switching_monitors.discard(monitor_name)

    def _find_monitor_window(self, monitor_name):
        for monitor, window in self.windows.items():
            if monitor.get_model() == monitor_name:
                return monitor, window
        return None, None

    def _playlist_tick(self, monitor_name):
        if not self._is_instant_playlist_mode():
            return False
        monitor, window = self._find_monitor_window(monitor_name)
        if monitor is None or window is None:
            return False
        if not self._should_playback_start():
            return True
        length_ms = window.get_length()
        position_ms = window.get_time()
        if length_ms <= 0 or position_ms < 0:
            return True
        remaining_ms = length_ms - position_ms
        if remaining_ms > INSTANT_PLAYLIST_SWITCH_LEAD_MS:
            return True
        self._transition_to_queued_media(monitor, window)
        return True

    def _start_instant_playlist_transitions(self):
        self._stop_playlist_timers()
        if self.mode != MODE_VIDEO or not bool(self.config.get(CONFIG_KEY_PLAYLIST, False)):
            return
        for monitor, window in self.windows.items():
            monitor_name = monitor.get_model()
            current_source = self._get_source_for_monitor(
                monitor_name, self.config.get(CONFIG_KEY_DATA_SOURCE, {})
            )
            self._prepare_next_media(monitor, window, current_source)
        if not self._is_instant_playlist_mode():
            logger.info("[Playlist] Preloaded next videos for timer-based transitions")
            return
        for monitor, window in self.windows.items():
            monitor_name = monitor.get_model()
            timer_id = GLib.timeout_add(
                INSTANT_PLAYLIST_POLL_INTERVAL_MS,
                self._playlist_tick,
                monitor_name,
            )
            self._playlist_monitor_timers[monitor_name] = timer_id
        logger.info("[Playlist] Seamless transitions enabled (0.5s preload lead)")

    @property
    def mode(self):
        return self.config[CONFIG_KEY_MODE]

    @property
    def data_source(self):
        return self.config[CONFIG_KEY_DATA_SOURCE]

    @data_source.setter
    def data_source(self, data_source):
        self.config[CONFIG_KEY_DATA_SOURCE] = data_source
        self._stop_playlist_timers()

        if self.mode == MODE_VIDEO:
            loop_video = not (
                bool(self.config.get(CONFIG_KEY_PLAYLIST, False))
                and int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300)) == 0
            )
            for monitor, window in self.windows.items():
                monitor_name = monitor.get_model()
                source = self._get_source_for_monitor(monitor_name, data_source)
                if not source:
                    logger.warning(f"[Playlist] Empty source for {monitor_name}; skipping")
                    window.clear_queued_media()
                    continue
                logger.info(f"Setting source {source} to {monitor.get_model()}")
                media = self._create_video_media(
                    window=window,
                    source=source,
                    loop_video=loop_video,
                    disable_audio=not monitor.is_primary(),
                )
                window.set_media(media)
                window.set_position(0.0)
                video_width, video_height = self._probe_video_dimensions(source)
                window.centercrop(video_width, video_height)

        elif self.mode == MODE_STREAM:
            source = data_source['Default']
            formats = get_formats(source)
            max_height = max(
                self.windows, key=lambda m: m.get_geometry().height).get_geometry().height
            video_url, video_width, video_height = get_optimal_video(
                formats, max_height)
            audio_url = get_best_audio(formats)

            for monitor, window in self.windows.items():
                media = window.media_new(video_url)
                media.add_option("input-repeat=65535")
                window.set_media(media)
                if monitor.is_primary():
                    window.add_audio_track(audio_url)
                else:
                    # `get_optimal_video` now might return video with audio.
                    media.add_option("no-audio")
                window.set_position(0.0)
                window.centercrop(video_width, video_height)
        else:
            raise ValueError("Invalid mode")

        self.volume = self.config[CONFIG_KEY_VOLUME]
        self.is_mute = self.config[CONFIG_KEY_MUTE]
        self.start_playback()

        # Everything is initialized. Create handlers if haven't (singleton pattern).
        if not self.active_handler:
            self.active_handler = ActiveHandler(self._on_active_changed)
        if not self.window_handler and not is_wayland():
            # Only create WindowHandler on X11, not Wayland
            self.window_handler = WindowHandler(self._on_window_state_changed)

        playlist_interval = int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300))
        playlist_enabled = bool(self.config.get(CONFIG_KEY_PLAYLIST, False))
        if self.config[CONFIG_KEY_STATIC_WALLPAPER] and self.mode == MODE_VIDEO and not (
            playlist_enabled and playlist_interval == 0
        ):
            self.set_static_wallpaper()
        elif self.config[CONFIG_KEY_STATIC_WALLPAPER] and playlist_enabled and playlist_interval == 0:
            # Static wallpaper extraction/blur is expensive; skip it in instant playlist mode.
            self.set_original_wallpaper()
        else:
            self.set_original_wallpaper()

        self._start_instant_playlist_transitions()

    @property
    def volume(self):
        return self.config[CONFIG_KEY_VOLUME]

    @volume.setter
    def volume(self, volume):
        self.config[CONFIG_KEY_VOLUME] = volume
        for monitor in self.windows:
            if monitor.is_primary():
                self.windows[monitor].set_volume(volume)

    @property
    def is_mute(self):
        return self.config[CONFIG_KEY_MUTE]

    @is_mute.setter
    def is_mute(self, is_mute):
        self.config[CONFIG_KEY_MUTE] = is_mute
        for monitor, window in self.windows.items():
            if monitor.is_primary():
                window.set_mute(is_mute)

    @property
    def is_playing(self):
        return not self.is_paused_by_user

    def pause_playback(self):
        for monitor, window in self.windows.items():
            window.pause_fade(fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                              fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL])

    def start_playback(self):
        if self._should_playback_start():
            for monitor, window in self.windows.items():
                window.play_fade(target=self.volume, fade_duration_sec=self.config[CONFIG_KEY_FADE_DURATION_SEC],
                            fade_interval=self.config[CONFIG_KEY_FADE_INTERVAL])

    def playlist_next(self):
        if self.mode != MODE_VIDEO or not bool(self.config.get(CONFIG_KEY_PLAYLIST, False)):
            return
        for monitor, window in self.windows.items():
            self._transition_to_queued_media(monitor, window)

    def apply_video_config(self):
        self.reload_config()
        if self.mode == MODE_VIDEO:
            self.data_source = self.config[CONFIG_KEY_DATA_SOURCE]

    def monitor_sync(self):
        primary_monitor = None
        for monitor, window in self.windows.items():
            if monitor.is_primary:
                primary_monitor = monitor
                break
        if primary_monitor:
            for monitor, window in self.windows.items():
                if monitor == primary_monitor:
                    continue
                # `set_position()` method require the playback to be enabled before calling
                window.play()
                window.set_position(
                    self.windows[primary_monitor].get_position())
                window.play() if self.windows[primary_monitor].is_playing(
                ) else window.pause()

    def set_static_wallpaper(self):
        # Currently for GNOME only
        if not is_gnome():
            return
        # Get the duration of the video
        try:
            duration = float(subprocess.check_output([
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', self.data_source['Default']
            ], shell = False))
        except subprocess.CalledProcessError:
            duration = 0
        # Find the golden ratio
        ss = time.strftime('%H:%M:%S', time.gmtime(duration / 3.14))
        # Extract the frame
        static_wallpaper_path = os.path.join(
            CONFIG_DIR, "static-{:06d}.png".format(random.randint(0, 999999)))
        ret = subprocess.run([
            'ffmpeg', '-y', '-ss', ss, '-i', self.data_source['Default'],
            '-vframes', '1', static_wallpaper_path
        ], shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if ret.returncode == 0 and os.path.isfile(static_wallpaper_path):
            blur_wallpaper = Image.open(static_wallpaper_path)
            blur_wallpaper = blur_wallpaper.filter(
                ImageFilter.GaussianBlur(self.config["static_wallpaper_blur_radius"]))
            blur_wallpaper.save(static_wallpaper_path)
            static_wallpaper_uri = pathlib.Path(
                static_wallpaper_path).resolve().as_uri()
            if is_flatpak():
                try:
                    subprocess.run(
                        ['flatpak-spawn', '--host', 'gsettings', 'set', 'org.gnome.desktop.background', 'picture-uri', static_wallpaper_uri], shell=False)
                    subprocess.run(
                        ['flatpak-spawn', '--host', 'gsettings', 'set', 'org.gnome.desktop.background', 'picture-uri-dark', static_wallpaper_uri], shell=False)
                except subprocess.CalledProcessError as e:
                    logger.error(f"[StaticWallpaper] {e}")
            else:
                gso = Gio.Settings.new("org.gnome.desktop.background")
                gso.set_string("picture-uri", static_wallpaper_uri)
                gso.set_string("picture-uri-dark", static_wallpaper_uri)

    def set_original_wallpaper(self):
        # Currently for GNOME only
        if not is_gnome():
            return
        if is_flatpak():
            try:
                if self.original_wallpaper_uri is not None:
                    subprocess.run(
                        ['flatpak-spawn', '--host', 'gsettings', 'set', 'org.gnome.desktop.background', 'picture-uri', self.original_wallpaper_uri], shell=False)
                if self.original_wallpaper_uri_dark is not None:
                    subprocess.run(
                        ['flatpak-spawn', '--host', 'gsettings', 'set', 'org.gnome.desktop.background', 'picture-uri-dark', self.original_wallpaper_uri], shell=False)
            except subprocess.CalledProcessError as e:
                logger.error(f"[StaticWallpaper] {e}")
        else:
            gso = Gio.Settings.new("org.gnome.desktop.background")
            gso.set_string("picture-uri", self.original_wallpaper_uri)
            gso.set_string("picture-uri-dark",
                           self.original_wallpaper_uri_dark)
        # Purge the generated static wallpaper (and leftover if any)
        for f in glob.glob(os.path.join(CONFIG_DIR, "static-*.png")):
            os.remove(f)

    def reload_config(self):
        self.config = ConfigUtil().load()

    def quit_player(self):
        self._stop_playlist_timers()
        self.set_original_wallpaper()
        
        # Cleanup handlers
        if self.active_handler:
            self.active_handler.cleanup()
            self.active_handler = None
            
        if self.window_handler:
            self.window_handler.cleanup()
            self.window_handler = None
        
        # Cleanup all windows
        for monitor, window in self.windows.items():
            if window:
                window.cleanup()
        
        super().quit_player()


def main():
    bus = SessionBus()
    app = VideoPlayer()
    try:
        bus.publish(DBUS_NAME_PLAYER, app)
    except RuntimeError as e:
        logger.error(e)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
