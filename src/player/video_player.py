import sys
import glob
import time
import random
import ctypes
import logging
import pathlib
import hashlib
import json
import subprocess
import threading
import shutil

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, Gdk, GLib

import vlc

try:
    import os
    sys.path.insert(1, os.path.join(sys.path[0], '..'))
    from player.base_player import BasePlayer
    from player.fit_geometry import (
        calculate_fit_geometry,
        normalize_fit_mode,
    )
    from ipc import publish_service
    from menu import build_menu
    from commons import *
    from utils import ActiveHandler, ConfigUtil, is_gnome, is_wayland, is_nvidia_proprietary, is_vdpau_ok, is_flatpak, get_vlc_hwdec_profile, probe_media_info, is_usable_video_path, build_reverse_video
except (ModuleNotFoundError, ImportError):
    from wallblazer.player.base_player import BasePlayer
    from wallblazer.player.fit_geometry import (
        calculate_fit_geometry,
        normalize_fit_mode,
    )
    from wallblazer.ipc import publish_service
    from wallblazer.menu import build_menu
    from wallblazer.commons import *
    from wallblazer.utils import ActiveHandler, ConfigUtil, is_gnome, is_wayland, is_nvidia_proprietary, is_vdpau_ok, is_flatpak, get_vlc_hwdec_profile, probe_media_info, is_usable_video_path, build_reverse_video

logger = logging.getLogger(LOGGER_NAME)

INSTANT_PLAYLIST_SWITCH_LEAD_MS = 500
INSTANT_PLAYLIST_POLL_INTERVAL_MS = 100

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_LOW_END_MODE_ENV = "WALLBLAZER_LOW_END_MODE"
_REVERSE_RETRY_COOLDOWN_SEC = 5.0
# Keep reverse cache at source resolution to preserve visual quality.
_REVERSE_FILTER = "reverse"
_REVERSE_X264_PRESET = "medium"
_REVERSE_X264_CRF = "16"


def _is_truthy_env(var_name):
    value = str(os.environ.get(var_name, "")).strip().lower()
    return value in _TRUTHY_ENV_VALUES


def _load_stream_helpers():
    try:
        from yt_utils import get_formats, get_best_audio, get_optimal_video
    except (ModuleNotFoundError, ImportError):
        from wallblazer.yt_utils import get_formats, get_best_audio, get_optimal_video
    return get_formats, get_best_audio, get_optimal_video


def _build_vlc_options():
    cpu_total = max(1, int(os.cpu_count() or 1))
    low_end_mode = _is_truthy_env(_LOW_END_MODE_ENV) or cpu_total <= 2
    decode_threads = min(4, cpu_total)
    if cpu_total <= 2:
        decode_threads = 1
    elif cpu_total <= 4:
        decode_threads = 2
    if low_end_mode:
        decode_threads = 1

    # Shared baseline options — favor hardware-decoded quality first, then
    # trim buffers and worker counts so wallpaper playback stays lightweight.
    options = [
        "--no-disable-screensaver",
        "--no-video-title-show",
        "--no-osd",
        "--no-spu",
        "--no-stats",
        f"--avcodec-threads={decode_threads}",
        "--drop-late-frames",
        "--file-caching=220" if low_end_mode else "--file-caching=750",
        "--live-caching=100" if low_end_mode else "--live-caching=250",
        "--disc-caching=100" if low_end_mode else "--disc-caching=250",
        "--network-caching=300" if low_end_mode else "--network-caching=1000",
    ]

    if low_end_mode:
        # Keep low-end mode conservative to avoid visible decode corruption.
        options.extend([
            "--avcodec-fast",
            "--clock-jitter=0",
            "--clock-synchro=0",
            "--skip-frames",
            "--avcodec-skip-frame=2",
            "--avcodec-skip-idct=2",
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


class CropViewport(Gtk.Fixed):
    """A fixed-size, clipping viewport for an oversize VLC child widget."""

    __gtype_name__ = "WallBlazerCropViewport"

    def __init__(self, width, height):
        super().__init__()
        self._viewport_width = max(1, int(width))
        self._viewport_height = max(1, int(height))
        self.set_size_request(self._viewport_width, self._viewport_height)

    def set_viewport_size(self, width, height):
        self._viewport_width = max(1, int(width))
        self._viewport_height = max(1, int(height))
        self.set_size_request(self._viewport_width, self._viewport_height)
        self.queue_resize()

    def do_get_preferred_width(self):
        return self._viewport_width, self._viewport_width

    def do_get_preferred_height(self):
        return self._viewport_height, self._viewport_height


class VLCWidget(Gtk.EventBox):
    """
    Simple VLC widget.
    Its player can be controlled through the 'player' attribute, which
    is a vlc.MediaPlayer() instance.
    """
    __gtype_name__ = "VLCWidget"

    def __init__(self, width, height):
        Gtk.EventBox.__init__(self)
        # VLC renders directly into this widget's native GDK window.
        # Disable GTK background painting here to avoid black output overriding VLC.
        self.set_visible_window(True)
        self.set_app_paintable(True)
        self.connect("draw", lambda widget, cr: True)

        # Spawn a VLC instance and create a new media player to embed.
        # Hide WAYLAND_DISPLAY so VLC doesn't try to use Wayland output which fails with XID embedding.
        if sys.platform != "win32" and os.environ.get("DISPLAY"):
            os.environ.pop("WAYLAND_DISPLAY", None)
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
                gdk_window = self.get_window()
                if gdk_window is None:
                    logger.warning("[VLC] GTK window is unavailable during embed")
                    return False
                try:
                    gdk_window.ensure_native()
                except Exception:
                    pass
                xid = gdk_window.get_xid()
                if not xid:
                    # XID not ready yet — retry after GTK has had a chance to
                    # finish realizing the native window (typically < 100 ms).
                    logger.warning("[VLC] XID=0 at realize-time, scheduling retry in 150ms")
                    GLib.timeout_add(150, handle_embed)
                    return False
                logger.info(f"[VLC] Embedding into XID={xid}")
                self.player.set_xwindow(xid)
            return False

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


class ColorTintOverlay(Gtk.DrawingArea):
    _TINT_CHANNEL_KEYS = ("red", "green", "blue", "yellow", "cyan", "magenta")

    def __init__(self):
        super().__init__()
        self._adjustments = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_no_show_all(True)
        self.connect("draw", self._on_draw)
        # Keep overlay hidden until at least one tint channel is active.
        self.set_visible(False)

    def set_adjustments(self, adjustments):
        values = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        if isinstance(adjustments, dict):
            for key in values.keys():
                try:
                    if key in adjustments:
                        values[key] = float(adjustments[key])
                except (TypeError, ValueError):
                    pass
        self._adjustments = values
        active = self._has_active_tint(values)
        self.set_visible(active)
        if active:
            self.queue_draw()

    @classmethod
    def _has_active_tint(cls, values):
        for key in cls._TINT_CHANNEL_KEYS:
            if abs(float(values.get(key, 0.0))) > 1e-3:
                return True
        return False

    def _iter_layers(self):
        complement = {
            "red": "cyan",
            "green": "magenta",
            "blue": "yellow",
            "yellow": "blue",
            "cyan": "red",
            "magenta": "green",
        }
        colors = {
            "red": (1.0, 0.0, 0.0),
            "green": (0.0, 1.0, 0.0),
            "blue": (0.0, 0.15, 1.0),
            "yellow": (1.0, 0.85, 0.0),
            "cyan": (0.0, 0.85, 1.0),
            "magenta": (1.0, 0.0, 0.85),
        }
        for key, rgba in colors.items():
            value = float(self._adjustments.get(key, 0.0))
            if abs(value) < 1e-3:
                continue
            color_key = key if value >= 0 else complement[key]
            alpha = min(0.28, abs(value) * 0.28)
            yield (*colors[color_key], alpha)

    def _on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        drew = False
        for red, green, blue, alpha in self._iter_layers():
            if alpha > 0.0:
                cr.set_source_rgba(red, green, blue, alpha)
                cr.rectangle(0, 0, alloc.width, alloc.height)
                cr.fill()
                drew = True
        return drew


class PlayerWindow(Gtk.ApplicationWindow):
    def __init__(self, name, width, height, *args, **kwargs):
        super(PlayerWindow, self).__init__(*args, **kwargs)
        # Setup a VLC widget given the provided width and height.
        self.width = width
        self.height = height
        self.name = name
        self.__vlc_widget = VLCWidget(width, height)
        self._viewport = CropViewport(width, height)
        self._viewport.put(self.__vlc_widget, 0, 0)
        self._overlay = Gtk.Overlay()
        self._overlay.add(self._viewport)
        self._color_overlay = ColorTintOverlay()
        self._overlay.add_overlay(self._color_overlay)
        self._overlay.set_overlay_pass_through(self._color_overlay, True)
        self.add(self._overlay)
        self._overlay.show_all()

        # These are to allow us to right click. VLC can't hijack mouse input, and probably not key inputs either in
        # Case we want to add keyboard shortcuts later on.
        self.__vlc_widget.player.video_set_mouse_input(False)
        self.__vlc_widget.player.video_set_key_input(False)

        # A timer that handling fade-in/out
        self.fade = Fade()
        self._queued_media = None
        self._queued_source = None
        self._queued_base = None
        self._queued_phase = "forward"
        self._queued_dimensions = (None, None)
        self._active_media = None
        self._media_generation = 0
        self._end_reached_event_manager = None
        self._end_reached_event_callback = None
        self._fit_mode = "cover"
        self._playback_rate = 1.0
        self._rate_apply_source_id = None
        self._adjust_apply_source_id = None
        self._video_adjustments = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        self._warned_unsafe_vlc_adjustments = False
        self._is_disposed = False

        self._attach_end_reached_handler()

        self.menu = None
        self.connect("button-press-event", self._on_button_press_event)
        self.connect("configure-event", self._on_configure_event)

    @staticmethod
    def _clear_vlc_string_option(setter):
        try:
            setter(None)
        except TypeError:
            setter("")

    def _vlc_player(self):
        if self._is_disposed or self.__vlc_widget is None:
            return None
        return getattr(self.__vlc_widget, "player", None)

    def _attach_end_reached_handler(self):
        """Forward VLC's end event onto GTK's main loop for instant playlists."""
        player = self._vlc_player()
        if player is None:
            return
        try:
            manager = player.event_manager()
            callback = self._on_media_end_reached
            manager.event_attach(vlc.EventType.MediaPlayerEndReached, callback)
            self._end_reached_event_manager = manager
            self._end_reached_event_callback = callback
        except Exception as e:
            logger.warning(f"[Playlist] Could not attach VLC end callback: {e}")

    def _on_media_end_reached(self, _event):
        # libVLC invokes event callbacks from its own thread.  Do not call
        # libVLC or GTK from here; dispatch the work onto the GTK main loop.
        if self._is_disposed:
            return
        generation = self._media_generation
        try:
            GLib.idle_add(self._dispatch_media_end_reached, generation)
        except Exception as e:
            logger.debug(f"[Playlist] Could not schedule VLC end callback: {e}")

    def _dispatch_media_end_reached(self, generation):
        # A pre-emptive poll transition may have already replaced this media.
        # Ignore its late event rather than advancing the newly started clip.
        if self._is_disposed or generation != self._media_generation:
            return False
        app = self.get_application()
        if app is not None and hasattr(app, "on_window_end_reached"):
            app.on_window_end_reached(self.name)
        return False

    def update_monitor_geometry(self, x, y, width, height):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._viewport.set_viewport_size(self.width, self.height)
        self.resize(self.width, self.height)
        self.move(int(x), int(y))
        self.schedule_centercrop()

    def _on_configure_event(self, _widget, event):
        width = max(1, int(getattr(event, "width", self.width)))
        height = max(1, int(getattr(event, "height", self.height)))
        if width != self.width or height != self.height:
            self.width = width
            self.height = height
            self._viewport.set_viewport_size(width, height)
            self.schedule_centercrop()
        return False

    def set_fit_mode(self, fit_mode):
        self._fit_mode = normalize_fit_mode(fit_mode)
        self.schedule_centercrop()

    def set_playback_rate(self, rate, apply_now=True):
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 1.0
        self._playback_rate = max(0.25, min(4.0, rate))
        if apply_now:
            self._schedule_rate_apply()

    def _schedule_rate_apply(self, delay_ms=120):
        if self._rate_apply_source_id is not None:
            try:
                GLib.source_remove(self._rate_apply_source_id)
            except Exception:
                pass
        self._rate_apply_source_id = GLib.timeout_add(delay_ms, self._apply_playback_rate)

    def _apply_playback_rate(self):
        self._rate_apply_source_id = None
        try:
            self.__vlc_widget.player.set_rate(self._playback_rate)
        except Exception as e:
            logger.debug(f"[PlaybackRate] Could not apply rate={self._playback_rate}: {e}")
        return False

    def apply_video_adjustments(self, adjustments):
        values = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        if isinstance(adjustments, dict):
            for key in values.keys():
                try:
                    if key in adjustments:
                        values[key] = float(adjustments[key])
                except (TypeError, ValueError):
                    pass
        self._video_adjustments = values
        player = self.__vlc_widget.player
        supported_active = any(
            abs(values[key] - DEFAULT_VIDEO_ADJUSTMENTS[key]) > 1e-3
            for key in ("brightness", "contrast", "saturation", "gamma")
        ) or abs(values["hue"]) > 1e-3

        # Safety guard for Linux/X11 + VLC:
        # some VLC output/driver combinations apply these knobs through
        # display-level controls instead of per-video processing, which can
        # tint/dim the entire desktop. Keep wallpaper-local behavior by
        # disabling native VLC image adjustments in this environment.
        is_linux_x11 = (
            sys.platform != "win32"
            and str(os.environ.get("XDG_SESSION_TYPE", "")).strip().lower() == "x11"
        )
        if is_linux_x11:
            try:
                player.video_set_adjust_int(vlc.VideoAdjustOption.Enable, 0)
            except Exception:
                pass
            if supported_active and not self._warned_unsafe_vlc_adjustments:
                logger.warning(
                    "[VideoAdjust] Skipping VLC brightness/contrast/saturation/hue/gamma on X11 "
                    "to avoid display-wide changes. Use GStreamer backend for wallpaper-only adjustments."
                )
                self._warned_unsafe_vlc_adjustments = True
            self._color_overlay.set_adjustments(values)
            return

        try:
            player.video_set_adjust_int(vlc.VideoAdjustOption.Enable, 1 if supported_active else 0)
            player.video_set_adjust_float(vlc.VideoAdjustOption.Brightness, values["brightness"])
            player.video_set_adjust_float(vlc.VideoAdjustOption.Contrast, values["contrast"])
            player.video_set_adjust_float(vlc.VideoAdjustOption.Saturation, values["saturation"])
            player.video_set_adjust_float(vlc.VideoAdjustOption.Gamma, values["gamma"])
            player.video_set_adjust_int(vlc.VideoAdjustOption.Hue, int(round(values["hue"])) % 360)
        except Exception as e:
            logger.debug(f"[VideoAdjust] Could not apply VLC adjustments: {e}")
        self._color_overlay.set_adjustments(values)

    def _reapply_video_adjustments(self):
        self._adjust_apply_source_id = None
        self.apply_video_adjustments(self._video_adjustments)
        return False

    def _schedule_adjustment_reapply(self, delay_ms=180):
        if self._adjust_apply_source_id is not None:
            try:
                GLib.source_remove(self._adjust_apply_source_id)
            except Exception:
                pass
        self._adjust_apply_source_id = GLib.timeout_add(delay_ms, self._reapply_video_adjustments)

    def play(self):
        rc = self.__vlc_widget.player.play()
        logger.info(f"[VLC] play() rc={rc} media={self._active_media is not None}")
        self._schedule_rate_apply()
        self._schedule_adjustment_reapply()
        GLib.timeout_add(1000, self._log_playback_state_once)

    def _log_playback_state_once(self):
        try:
            state = self.__vlc_widget.player.get_state()
            logger.info(
                f"[VLC] state={state} is_playing={self.__vlc_widget.player.is_playing()} "
                f"time={self.__vlc_widget.player.get_time()} length={self.__vlc_widget.player.get_length()}"
            )
        except Exception as e:
            logger.warning(f"[VLC] Could not read playback state: {e}")
        return False

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
        if args:
            self._active_media = args[0]
            self._media_generation += 1
        self._clear_vlc_string_option(self.__vlc_widget.player.video_set_aspect_ratio)
        self._clear_vlc_string_option(self.__vlc_widget.player.video_set_crop_geometry)
        self.__vlc_widget.player.set_media(*args)

    def queue_media(self, media, source, base_source=None, phase="forward", video_width=None, video_height=None):
        self._queued_media = media
        self._queued_source = source
        self._queued_base = base_source if base_source else source
        self._queued_phase = phase
        self._queued_dimensions = (video_width, video_height)

    def queued_source(self):
        return self._queued_source

    def queued_base(self):
        return self._queued_base

    def queued_phase(self):
        return self._queued_phase

    def clear_queued_media(self):
        self._queued_media = None
        self._queued_source = None
        self._queued_base = None
        self._queued_phase = "forward"
        self._queued_dimensions = (None, None)

    def switch_to_queued_media(self, should_play=True):
        if not self._queued_media:
            return None, None, None
        media = self._queued_media
        source = self._queued_source
        base_source = self._queued_base if self._queued_base else source
        phase = self._queued_phase
        video_width, video_height = self._queued_dimensions
        self.clear_queued_media()
        try:
            # Force VLC to detach the current media before swapping.
            # Without this, libVLC can keep the old clip active and ignore
            # the queued reverse/next media during seamless transitions.
            self.__vlc_widget.player.stop()
        except Exception as e:
            logger.debug(f"[Playlist] stop() before switch failed: {e}")
        self.set_media(media)
        self.schedule_centercrop(video_width, video_height)
        if should_play:
            self.play()
        logger.info(
            f"[Playlist] Switched media source={source} base={base_source} phase={phase}"
        )
        return source, base_source, phase

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
        player = self._vlc_player()
        if player is None:
            # Shutdown race: timers can fire after cleanup.
            return True
        # Getting dimension from libvlc is not reliable enough (need to consider timing)
        if (video_width, video_height) == (None, None):
            video_width, video_height = player.video_get_size()
            if video_width == 0 or video_height == 0:
                logger.warning("[CenterCrop] video_get_size is not ready yet")
                return False
        logger.debug(f"[CenterCrop] Dimension {video_width}x{video_height}")

        # Keep VLC in autoscale mode inside a precisely-sized child widget.  A
        # Gtk.Fixed viewport then clips the oversize child for cover mode,
        # which is reliable across VLC output modules and keeps the crop
        # centered without any letterboxing.
        player.video_set_scale(0)
        fit_mode = self._fit_mode
        if fit_mode == "stretch":
            self.__vlc_widget.set_size_request(self.width, self.height)
            self._viewport.move(self.__vlc_widget, 0, 0)
            self._clear_vlc_string_option(player.video_set_crop_geometry)
            player.video_set_aspect_ratio(f"{self.width}:{self.height}")
            return True

        self._clear_vlc_string_option(player.video_set_aspect_ratio)
        self._clear_vlc_string_option(player.video_set_crop_geometry)
        geometry = calculate_fit_geometry(
            self.width,
            self.height,
            video_width,
            video_height,
            fit_mode,
        )
        logger.debug(
            f"[CenterCrop] {fit_mode} geometry: "
            f"{geometry.width}x{geometry.height}+{geometry.x}+{geometry.y}"
        )
        self.__vlc_widget.set_size_request(geometry.width, geometry.height)
        self._viewport.move(self.__vlc_widget, geometry.x, geometry.y)
        return True

    def schedule_centercrop(self, video_width=None, video_height=None, attempts=18, delay_ms=120):
        def _try_crop(remaining):
            if self._is_disposed:
                return False
            try:
                done = self.centercrop(video_width, video_height)
            except Exception as e:
                logger.debug(f"[CenterCrop] Retry aborted due to error: {e}")
                return False
            if done or remaining <= 0:
                return False
            GLib.timeout_add(delay_ms, _try_crop, remaining - 1)
            return False

        GLib.timeout_add(delay_ms, _try_crop, attempts)

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
        self._is_disposed = True
        self.fade.cancel()
        self.clear_queued_media()
        self._active_media = None
        if self._end_reached_event_manager is not None:
            try:
                self._end_reached_event_manager.event_detach(vlc.EventType.MediaPlayerEndReached)
            except Exception:
                pass
            self._end_reached_event_manager = None
            self._end_reached_event_callback = None
        if self._rate_apply_source_id is not None:
            try:
                GLib.source_remove(self._rate_apply_source_id)
            except Exception:
                pass
            self._rate_apply_source_id = None
        if self._adjust_apply_source_id is not None:
            try:
                GLib.source_remove(self._adjust_apply_source_id)
            except Exception:
                pass
            self._adjust_apply_source_id = None
        if self.__vlc_widget:
            self.__vlc_widget.cleanup()

    def media_event_manager(self):
        player = self._vlc_player()
        if player is None:
            return None
        return player.event_manager()


class VideoPlayer(BasePlayer):
    """
    <node>
    <interface name='io.github.wallblazer.wallblazer.player'>
        <property name="mode" type="s" access="read"/>
        <property name="data_source" type="a{ss}" access="readwrite"/>
        <property name="volume" type="i" access="readwrite"/>
        <property name="is_mute" type="b" access="readwrite"/>
        <property name="is_playing" type="b" access="read"/>
        <property name="is_paused_by_user" type="b" access="readwrite"/>
        <method name='reload_config'/>
        <method name='apply_video_config'/>
        <method name='apply_video_profile'/>
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
        self._video_dimension_inflight = set()
        self._reverse_state = {}
        self._reverse_cache_dir = os.path.join(CONFIG_DIR, "reverse-cache")
        self._reverse_jobs = {}
        self._reverse_lock = threading.Lock()
        self._reverse_build_semaphore = threading.BoundedSemaphore(1)
        self._reverse_failed_builds = {}
        self._transition_trace_file = os.environ.get("WALLBLAZER_TRACE_FILE", "").strip()
        self._trace_throttle = {}
        self._playback_settle_until = {}

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
        return self._should_advance_playlist_on_end()

    def _get_source_for_monitor(self, monitor_name, data_source):
        monitor_source = data_source.get(monitor_name, "")
        if self.mode == MODE_VIDEO:
            if is_usable_video_path(monitor_source):
                return monitor_source
            default_source = data_source.get("Default", "")
            if is_usable_video_path(default_source):
                return default_source
            available_videos = get_video_paths()
            return available_videos[0] if available_videos else ""
        if isinstance(monitor_source, str) and monitor_source:
            return monitor_source
        default_source = data_source.get("Default", "")
        if isinstance(default_source, str):
            return default_source
        return ""

    def _get_monitor_playlist_name(self, monitor_name):
        assignments = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        if not isinstance(assignments, dict) or not isinstance(library, dict):
            return None
        playlist_name = assignments.get(monitor_name)
        if isinstance(playlist_name, str) and playlist_name in library:
            return playlist_name
        active = self.config.get(CONFIG_KEY_PLAYLIST_ACTIVE)
        if isinstance(active, str) and active in library:
            return active
        if library:
            return next(iter(library.keys()))
        return None

    def _reverse_items_for_playlist(self, playlist_name):
        reverse_items = self.config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if not isinstance(reverse_items, dict):
            return set()
        items = reverse_items.get(playlist_name, [])
        if not isinstance(items, list):
            return set()
        return set(item for item in items if isinstance(item, str))

    def _reverse_enabled_for_monitor(self, monitor_name, base_source):
        if not base_source or not isinstance(base_source, str):
            return False
        reverse_requested = False
        if bool(self.config.get(CONFIG_KEY_PLAYLIST, False)):
            if bool(self.config.get(CONFIG_KEY_REVERSE_PLAYLIST, False)):
                reverse_requested = True
            else:
                playlist_name = self._get_monitor_playlist_name(monitor_name)
                if not playlist_name:
                    return False
                reverse_requested = base_source in self._reverse_items_for_playlist(playlist_name)
        else:
            reverse_requested = bool(self.config.get(CONFIG_KEY_REVERSE_SINGLE, False))
        if not reverse_requested:
            return False
        return is_usable_video_path(base_source)

    def _fit_mode(self):
        return normalize_fit_mode(self.config.get(CONFIG_KEY_VIDEO_FIT_MODE, "cover"))

    def _video_adjustment_values(self):
        values = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        raw = self.config.get(CONFIG_KEY_VIDEO_ADJUSTMENTS, {})
        if isinstance(raw, dict):
            for key in values.keys():
                try:
                    if key in raw:
                        values[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
        return values

    def _playback_rate_for_phase(self, monitor_name, phase=None):
        if phase is None:
            phase = self._reverse_state.get(monitor_name, {}).get("phase", "forward")
        if phase == "reverse":
            raw = self.config.get(CONFIG_KEY_PLAYBACK_SPEED_REVERSE, 1.0)
        elif bool(self.config.get(CONFIG_KEY_PLAYLIST, False)):
            raw = self.config.get(CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST, 1.0)
        else:
            raw = self.config.get(CONFIG_KEY_PLAYBACK_SPEED_SINGLE, 1.0)
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            raw = 1.0
        return max(0.25, min(4.0, raw))

    def _apply_window_profile(self, monitor, window, phase=None):
        monitor_name = monitor.get_model()
        window.set_fit_mode(self._fit_mode())
        window.apply_video_adjustments(self._video_adjustment_values())
        window.set_playback_rate(self._playback_rate_for_phase(monitor_name, phase))

    def _should_advance_playlist_on_end(self):
        return (
            self.mode == MODE_VIDEO
            and bool(self.config.get(CONFIG_KEY_PLAYLIST, False))
            and int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300)) == 0
        )

    def _playlist_interval_seconds(self):
        try:
            return max(0, int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300)))
        except (TypeError, ValueError):
            return 300

    def _needs_transition_for_monitor(self, monitor_name, base_source):
        return self._should_advance_playlist_on_end() or self._reverse_enabled_for_monitor(
            monitor_name, base_source
        )

    def _reverse_cache_key(self, video_path):
        try:
            st = os.stat(video_path)
        except OSError:
            return None
        token = (
            f"v{REVERSE_CACHE_VERSION}|"
            f"{os.path.realpath(video_path)}|{st.st_size}|{int(st.st_mtime_ns)}"
        )
        return hashlib.sha1(token.encode("utf-8")).hexdigest()

    def _reverse_media_path(self, video_path):
        cache_key = self._reverse_cache_key(video_path)
        if not cache_key:
            return None
        return os.path.join(self._reverse_cache_dir, f"rev-{cache_key}.mp4")

    def _queue_reverse_build(self, video_path):
        if not isinstance(video_path, str) or not os.path.isfile(video_path):
            return None
        target = self._reverse_media_path(video_path)
        if not target:
            return None
        with self._reverse_lock:
            if video_path in self._reverse_jobs:
                return None
            if os.path.isfile(target) and os.path.getsize(target) > 0:
                return target
            self._reverse_jobs[video_path] = target

        def _worker():
            target_root, target_ext = os.path.splitext(target)
            tmp_target = f"{target_root}.tmp-{os.getpid()}-{threading.get_ident()}{target_ext or '.mp4'}"
            try:
                self._reverse_build_semaphore.acquire()
                if shutil.which("ffmpeg") is None:
                    logger.warning("[Reverse] ffmpeg not found; skipping reverse build")
                    return
                os.makedirs(self._reverse_cache_dir, exist_ok=True)
                if os.path.exists(tmp_target):
                    os.remove(tmp_target)
                build = build_reverse_video(
                    video_path,
                    tmp_target,
                    reverse_filter=_REVERSE_FILTER,
                    preset=_REVERSE_X264_PRESET,
                    crf=_REVERSE_X264_CRF,
                    pix_fmt="yuv420p",
                    threads=1,
                    low_priority=(sys.platform != "win32"),
                )
                if not build.get("ok"):
                    logger.warning(
                        f"[Reverse] Build failed for {video_path}: {build.get('error', 'unknown error')}"
                    )
                    self._reverse_failed_builds[video_path] = time.monotonic() + _REVERSE_RETRY_COOLDOWN_SEC
                    if os.path.exists(tmp_target):
                        os.remove(tmp_target)
                else:
                    self._reverse_failed_builds.pop(video_path, None)
                    os.replace(tmp_target, target)
                    logger.info(f"[Reverse] Cached reverse video at {target}")
            except Exception as e:
                logger.warning(f"[Reverse] Build error: {e}")
                self._reverse_failed_builds[video_path] = time.monotonic() + _REVERSE_RETRY_COOLDOWN_SEC
                try:
                    if os.path.exists(tmp_target):
                        os.remove(tmp_target)
                except OSError:
                    pass
            finally:
                self._reverse_build_semaphore.release()
                with self._reverse_lock:
                    self._reverse_jobs.pop(video_path, None)

        threading.Thread(target=_worker, daemon=True).start()
        return None

    def _get_reverse_media(self, video_path, schedule_if_missing=True):
        target = self._reverse_media_path(video_path)
        if not target:
            return None
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            with self._reverse_lock:
                build_in_progress = video_path in self._reverse_jobs
            if not build_in_progress:
                return target
        blocked_until = self._reverse_failed_builds.get(video_path)
        if blocked_until is not None and time.monotonic() < blocked_until:
            return None
        self._reverse_failed_builds.pop(video_path, None)
        if schedule_if_missing:
            self._queue_reverse_build(video_path)
        return None

    def _apply_dimensions_if_current(self, monitor_name, base_source, dims):
        monitor, window = self._find_monitor_window(monitor_name)
        if window is None:
            return False
        state = self._reverse_state.get(monitor_name)
        if not state or state.get("base") != base_source:
            return False
        window.schedule_centercrop(*dims)
        return False

    def _probe_video_dimensions(self, video_path, monitor_name=None):
        if not video_path:
            return (None, None)
        cached = self._video_dimension_cache.get(video_path)
        if cached is not None:
            return cached
        if video_path in self._video_dimension_inflight:
            return (None, None)
        self._video_dimension_inflight.add(video_path)

        def _worker():
            info = probe_media_info(video_path)
            parsed = (info.get("width"), info.get("height"))
            self._video_dimension_cache[video_path] = parsed
            self._video_dimension_inflight.discard(video_path)
            if monitor_name:
                GLib.idle_add(self._apply_dimensions_if_current, monitor_name, video_path, parsed)

        threading.Thread(target=_worker, daemon=True).start()
        return (None, None)

    def _create_video_media(self, window, source, loop_video, disable_audio):
        media = window.media_new(source)
        if loop_video:
            media.add_option("input-repeat=65535")
        media.add_option("no-video-title-show")
        # file-caching is set at the VLC instance level; skip per-media override
        if disable_audio:
            media.add_option("no-audio")
        return media

    def _next_effective_source(self, monitor_name, current_base, current_phase):
        """Compute the next (source, base, phase) to transition to.

        Ordering rules (both reverse and playlist enabled at interval=0):
          forward  → reverse of same video
          reverse  → forward of NEXT video in playlist
        If only reverse (no playlist advance): forward ↔ reverse, looping same video.
        If only playlist advance (no reverse): always forward of next video.
        """
        if not current_base:
            return (None, None, None)

        reverse_active = self._reverse_enabled_for_monitor(monitor_name, current_base)
        advance_playlist = self._should_advance_playlist_on_end()

        if current_phase == "forward":
            # Try to go to reverse first (if reverse enabled)
            if reverse_active:
                reverse_path = self._get_reverse_media(current_base, schedule_if_missing=True)
                if reverse_path:
                    return (reverse_path, current_base, "reverse")
                # If reverse build is currently in cooldown after a failure,
                # don't stall instant-playlist mode on the same item forever.
                blocked_until = self._reverse_failed_builds.get(current_base)
                if (
                    advance_playlist
                    and blocked_until is not None
                    and time.monotonic() < blocked_until
                ):
                    next_base = self._next_playlist_source(monitor_name, current_base)
                    if next_base:
                        return (next_base, next_base, "forward")
                # Reverse not cached yet — stay on forward until it's ready
                # (don't skip to next, that would lose the reverse phase)
                return (current_base, current_base, "forward")

            # No reverse: advance playlist if enabled
            if advance_playlist:
                next_base = self._next_playlist_source(monitor_name, current_base)
                if not next_base:
                    return (None, None, None)
                return (next_base, next_base, "forward")

            # Neither reverse nor playlist: loop same video
            return (current_base, current_base, "forward")

        else:  # current_phase == "reverse"
            # After playing reverse, advance to next video (or loop same if no playlist)
            if advance_playlist:
                next_base = self._next_playlist_source(monitor_name, current_base)
                if not next_base:
                    return (None, None, None)
                return (next_base, next_base, "forward")

            # No playlist advance: go back to forward of same video
            return (current_base, current_base, "forward")

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
        if isinstance(library, dict):
            playlist_name = self._get_monitor_playlist_name(monitor_name)
            if isinstance(playlist_name, str):
                videos = self._valid_playlist_entries(library.get(playlist_name, []))
                if videos:
                    return videos
        monitor_playlists = self.config.get(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        if isinstance(monitor_playlists, dict):
            return self._valid_playlist_entries(monitor_playlists.get(monitor_name, []))
        return []

    def _ordered_playlist_sources(self, monitor_name, current_base=None):
        videos = list(self._monitor_playlist_videos(monitor_name))
        if not videos:
            return []
        if current_base in videos:
            idx = videos.index(current_base)
            return videos[idx:] + videos[:idx]
        return videos

    def _reverse_targets_for_monitor(self, monitor_name, current_base=None, max_items=2):
        targets = []
        if bool(self.config.get(CONFIG_KEY_PLAYLIST, False)):
            ordered_videos = self._ordered_playlist_sources(monitor_name, current_base)
            if bool(self.config.get(CONFIG_KEY_REVERSE_PLAYLIST, False)):
                targets = ordered_videos
            else:
                reverse_set = self._reverse_items_for_playlist(
                    self._get_monitor_playlist_name(monitor_name)
                )
                targets = [video for video in ordered_videos if video in reverse_set]
        elif current_base and bool(self.config.get(CONFIG_KEY_REVERSE_SINGLE, False)):
            targets = [current_base]

        if max_items is None:
            return targets
        return targets[:max_items]

    def _prime_reverse_cache_for_monitor(self, monitor_name, current_base=None, max_items=2):
        for video_path in self._reverse_targets_for_monitor(
            monitor_name, current_base=current_base, max_items=max_items
        ):
            self._get_reverse_media(video_path, schedule_if_missing=True)

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

    def _prepare_next_media(self, monitor, window, current_base, current_phase):
        monitor_name = monitor.get_model()
        next_source, next_base, next_phase = self._next_effective_source(
            monitor_name, current_base, current_phase
        )
        if not next_source:
            window.clear_queued_media()
            return
        if (
            window.queued_source() == next_source
            and window.queued_base() == next_base
            and window.queued_phase() == next_phase
        ):
            return
        video_width, video_height = self._probe_video_dimensions(next_base, monitor_name)
        media = self._create_video_media(
            window=window,
            source=next_source,
            loop_video=False,
            disable_audio=not monitor.is_primary(),
        )
        window.queue_media(media, next_source, next_base, next_phase, video_width, video_height)
        self._trace_transition(
            "queued",
            monitor_name,
            source=next_source,
            base=next_base,
            phase=next_phase,
        )
        logger.debug(f"[Playlist] Preloaded next video for {monitor_name}: {next_source}")
        if next_base and self._reverse_enabled_for_monitor(monitor_name, next_base):
            self._prime_reverse_cache_for_monitor(monitor_name, current_base=next_base)

    def _persist_data_source(self):
        try:
            ConfigUtil().save(self.config)
        except Exception as e:
            logger.warning(f"[Playlist] Could not persist data source: {e}")

    def _trace_transition(
        self,
        event,
        monitor_name=None,
        throttle_key=None,
        throttle_sec=0.0,
        **fields,
    ):
        if not self._transition_trace_file:
            return
        if throttle_key:
            now = time.monotonic()
            last = self._trace_throttle.get(throttle_key)
            if last is not None and (now - last) < throttle_sec:
                return
            self._trace_throttle[throttle_key] = now
        record = {
            "ts": round(time.time(), 3),
            "event": event,
        }
        if monitor_name:
            record["monitor"] = monitor_name
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        try:
            trace_dir = os.path.dirname(self._transition_trace_file)
            if trace_dir:
                os.makedirs(trace_dir, exist_ok=True)
            with open(self._transition_trace_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[Trace] Could not write transition trace: {e}")

    def _transition_to_queued_media(self, monitor, window):
        monitor_name = monitor.get_model()
        if monitor_name in self._playlist_switching_monitors:
            return
        state = self._reverse_state.get(monitor_name)
        current_base = None
        current_phase = "forward"
        if state:
            current_base = state.get("base")
            current_phase = state.get("phase", "forward")
        if not current_base:
            current_base = self._get_source_for_monitor(
                monitor_name, self.config.get(CONFIG_KEY_DATA_SOURCE, {})
            )

        queued_source = window.queued_source()
        queued_base = window.queued_base()
        queued_phase = window.queued_phase()
        stale_forward_placeholder = (
            current_phase == "forward"
            and current_base
            and self._reverse_enabled_for_monitor(monitor_name, current_base)
            and queued_source == current_base
            and queued_base == current_base
            and queued_phase == "forward"
        )
        if stale_forward_placeholder:
            window.clear_queued_media()
            queued_source = None

        if not queued_source:
            self._prepare_next_media(monitor, window, current_base, current_phase)
            queued_source = window.queued_source()
            if not queued_source:
                return

        if queued_phase == "forward" and queued_base:
            self._prime_reverse_cache_for_monitor(monitor_name, current_base=queued_base, max_items=1)

        self._playlist_switching_monitors.add(monitor_name)
        try:
            switched_source, switched_base, switched_phase = window.switch_to_queued_media(
                should_play=self._should_playback_start()
            )
            if not switched_source:
                return
            if switched_base:
                self._reverse_state[monitor_name] = {
                    "base": switched_base,
                    "phase": switched_phase,
                }
                self._apply_window_profile(monitor, window, switched_phase)
                # VLC reports is_playing() == 0 briefly right after a media swap.
                # Without a short settle window, the next playlist tick can skip
                # the freshly switched reverse/forward phase immediately.
                self._playback_settle_until[monitor_name] = time.monotonic() + 2.5
                self._trace_transition(
                    "switched",
                    monitor_name,
                    source=switched_source,
                    base=switched_base,
                    phase=switched_phase,
                )
                # Persist only when the base source changes (avoid writing reverse-only phases)
                current_base = self._get_source_for_monitor(
                    monitor_name, self.config.get(CONFIG_KEY_DATA_SOURCE, {})
                )
                if switched_base != current_base:
                    self.config[CONFIG_KEY_DATA_SOURCE][monitor_name] = switched_base
                    if (
                        monitor_name == "Default"
                        or not self.config[CONFIG_KEY_DATA_SOURCE].get("Default")
                    ):
                        self.config[CONFIG_KEY_DATA_SOURCE]["Default"] = switched_base
                    self._persist_data_source()
                if self._reverse_enabled_for_monitor(monitor_name, switched_base):
                    max_reverse_items = 2 if self._playlist_interval_seconds() == 0 else 1
                    self._prime_reverse_cache_for_monitor(
                        monitor_name,
                        current_base=switched_base,
                        max_items=max_reverse_items,
                    )
            self._prepare_next_media(monitor, window, switched_base, switched_phase)
        finally:
            self._playlist_switching_monitors.discard(monitor_name)

    def on_window_end_reached(self, monitor_name):
        if self.mode != MODE_VIDEO:
            return False
        monitor, window = self._find_monitor_window(monitor_name)
        if monitor is None or window is None:
            return False
        state = self._reverse_state.get(monitor_name, {})
        current_base = state.get("base")
        if not current_base:
            current_base = self._get_source_for_monitor(
                monitor_name, self.config.get(CONFIG_KEY_DATA_SOURCE, {})
            )
        if not self._needs_transition_for_monitor(monitor_name, current_base):
            return False
        if not self._should_playback_start():
            self._trace_transition(
                "end_playback_blocked",
                monitor_name,
                throttle_key=f"{monitor_name}:end_playback_blocked",
                throttle_sec=1.0,
                pause_when_maximized=bool(self.config.get(CONFIG_KEY_PAUSE_WHEN_MAXIMIZED)),
                is_any_maximized=self.is_any_maximized,
                is_any_fullscreen=self.is_any_fullscreen,
                is_paused_by_user=self.is_paused_by_user,
            )
            return False
        self._trace_transition(
            "end_reached",
            monitor_name,
            base=current_base,
            phase=state.get("phase", "forward"),
            queued=window.queued_source(),
        )
        self._transition_to_queued_media(monitor, window)
        return False

    def _find_monitor_window(self, monitor_name):
        for monitor, window in self.windows.items():
            if monitor.get_model() == monitor_name:
                return monitor, window
        return None, None

    def _playlist_tick(self, monitor_name):
        self._trace_transition(
            "tick_enter",
            monitor_name,
            throttle_key=f"{monitor_name}:tick_enter",
            throttle_sec=1.0,
        )
        monitor, window = self._find_monitor_window(monitor_name)
        if monitor is None or window is None:
            return False
        state = self._reverse_state.get(monitor_name, {})
        current_base = state.get("base")
        if not current_base:
            current_base = self._get_source_for_monitor(
                monitor_name, self.config.get(CONFIG_KEY_DATA_SOURCE, {})
            )
        if not self._needs_transition_for_monitor(monitor_name, current_base):
            return False
        if not self._should_playback_start():
            self._trace_transition(
                "tick_playback_blocked",
                monitor_name,
                throttle_key=f"{monitor_name}:tick_playback_blocked",
                throttle_sec=1.0,
                pause_when_maximized=bool(self.config.get(CONFIG_KEY_PAUSE_WHEN_MAXIMIZED)),
                is_any_maximized=self.is_any_maximized,
                is_any_fullscreen=self.is_any_fullscreen,
                is_paused_by_user=self.is_paused_by_user,
            )
            return True
        settle_until = self._playback_settle_until.get(monitor_name, 0.0)
        if settle_until > time.monotonic():
            self._trace_transition(
                "tick_settling",
                monitor_name,
                throttle_key=f"{monitor_name}:tick_settling",
                throttle_sec=0.5,
                base=current_base,
                phase=state.get("phase", "forward"),
                queued=window.queued_source(),
            )
            return True
        length_ms = window.get_length()
        position_ms = window.get_time()
        if not window.is_playing():
            self._trace_transition(
                "tick_not_playing",
                monitor_name,
                throttle_key=f"{monitor_name}:tick_not_playing",
                throttle_sec=1.0,
                base=current_base,
                phase=state.get("phase", "forward"),
                queued=window.queued_source(),
            )
            self._transition_to_queued_media(monitor, window)
            return True
        if length_ms <= 0 or position_ms < 0:
            queued = window.queued_source()
            current_phase = state.get("phase", "forward")
            self._trace_transition(
                "tick_no_length",
                monitor_name,
                throttle_key=f"{monitor_name}:tick_no_length",
                throttle_sec=2.0,
                base=current_base,
                phase=current_phase,
                queued=queued,
                length_ms=length_ms,
                position_ms=position_ms,
            )
            if queued is None or (
                queued == current_base and current_phase == "forward"
                and self._reverse_enabled_for_monitor(monitor_name, current_base)
            ):
                self._prepare_next_media(monitor, window, current_base, current_phase)
            return True
        remaining_ms = length_ms - position_ms
        if remaining_ms > INSTANT_PLAYLIST_SWITCH_LEAD_MS:
            # Still playing — but if queued media is the same as current (reverse not ready yet),
            # try to preload again in case the reverse build completed.
            queued = window.queued_source()
            current_phase = state.get("phase", "forward")
            if queued is None or (
                queued == current_base and current_phase == "forward"
                and self._reverse_enabled_for_monitor(monitor_name, current_base)
            ):
                self._prepare_next_media(monitor, window, current_base, current_phase)
            return True
        self._trace_transition(
            "tick_transition_due",
            monitor_name,
            throttle_key=f"{monitor_name}:tick_transition_due",
            throttle_sec=0.5,
            base=current_base,
            phase=state.get("phase", "forward"),
            queued=window.queued_source(),
            length_ms=length_ms,
            position_ms=position_ms,
            remaining_ms=remaining_ms,
        )
        self._transition_to_queued_media(monitor, window)
        return True

    def _start_instant_playlist_transitions(self):
        self._stop_playlist_timers()
        if self.mode != MODE_VIDEO:
            return
        for monitor, window in self.windows.items():
            monitor_name = monitor.get_model()
            base_source = self._get_source_for_monitor(
                monitor_name, self.config.get(CONFIG_KEY_DATA_SOURCE, {})
            )
            if not base_source:
                window.clear_queued_media()
                continue
            state = self._reverse_state.get(monitor_name)
            if not state or state.get("base") != base_source:
                self._reverse_state[monitor_name] = {
                    "base": base_source,
                    "phase": "forward",
                }
                state = self._reverse_state[monitor_name]
            if self._needs_transition_for_monitor(monitor_name, base_source):
                self._prepare_next_media(monitor, window, base_source, state.get("phase", "forward"))
                timer_id = GLib.timeout_add(
                    INSTANT_PLAYLIST_POLL_INTERVAL_MS,
                    self._playlist_tick,
                    monitor_name,
                )
                self._playlist_monitor_timers[monitor_name] = timer_id
                self._trace_transition(
                    "timer_started",
                    monitor_name,
                    base=base_source,
                    phase=state.get("phase", "forward"),
                )
        if self._playlist_monitor_timers:
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
            for monitor, window in self.windows.items():
                monitor_name = monitor.get_model()
                base_source = self._get_source_for_monitor(monitor_name, data_source)
                if not base_source:
                    logger.warning(f"[Playlist] Empty source for {monitor_name}; skipping")
                    window.clear_queued_media()
                    self._reverse_state.pop(monitor_name, None)
                    continue
                self._reverse_state[monitor_name] = {
                    "base": base_source,
                    "phase": "forward",
                }
                reverse_active = self._reverse_enabled_for_monitor(monitor_name, base_source)
                # loop_video=True ONLY when no automatic advance is needed
                # (i.e. no playlist-end-advance AND no reverse mode)
                needs_transition = self._needs_transition_for_monitor(monitor_name, base_source)
                loop_video = not needs_transition
                logger.info(f"Setting source {base_source} to {monitor.get_model()} (loop={loop_video}, reverse={reverse_active})")
                media = self._create_video_media(
                    window=window,
                    source=base_source,
                    loop_video=loop_video,
                    disable_audio=not monitor.is_primary(),
                )
                window.set_media(media)
                self._apply_window_profile(monitor, window, "forward")
                window.set_position(0.0)
                video_width, video_height = self._probe_video_dimensions(base_source, monitor_name)
                window.schedule_centercrop(video_width, video_height)
                if reverse_active:
                    max_reverse_items = 2 if self._playlist_interval_seconds() == 0 else 1
                    self._prime_reverse_cache_for_monitor(
                        monitor_name,
                        current_base=base_source,
                        max_items=max_reverse_items,
                    )

        elif self.mode == MODE_STREAM:
            get_formats, get_best_audio, get_optimal_video = _load_stream_helpers()
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
                self._apply_window_profile(monitor, window, "forward")
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
                phase = self._reverse_state.get(monitor.get_model(), {}).get("phase", "forward")
                self._apply_window_profile(monitor, window, phase)
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

    def apply_video_profile(self):
        self.reload_config()
        if self.mode != MODE_VIDEO:
            return
        for monitor, window in self.windows.items():
            phase = self._reverse_state.get(monitor.get_model(), {}).get("phase", "forward")
            self._apply_window_profile(monitor, window, phase)
            window.schedule_centercrop()

    def monitor_sync(self):
        primary_monitor = None
        for monitor, window in self.windows.items():
            if monitor.is_primary():
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
        from PIL import Image, ImageFilter
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
    logging.basicConfig(level=logging.INFO)
    app = VideoPlayer()
    try:
        service_handle = publish_service(DBUS_NAME_PLAYER, app)
    except Exception as e:
        logger.error(e)
        service_handle = None
    app.run(sys.argv)


if __name__ == "__main__":
    main()
