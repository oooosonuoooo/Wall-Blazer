import logging
import os
import pathlib
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gdk, GLib, Gst

try:
    import os
    sys.path.insert(1, os.path.join(sys.path[0], ".."))
    from commons import *
    from ipc import publish_service
    from menu import build_menu
    from player.fit_geometry import calculate_fit_geometry, normalize_fit_mode
    from player.video_player import VideoPlayer
    from utils import get_vlc_hwdec_profile, probe_media_info
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import *
    from wallblazer.ipc import publish_service
    from wallblazer.menu import build_menu
    from wallblazer.player.fit_geometry import calculate_fit_geometry, normalize_fit_mode
    from wallblazer.player.video_player import VideoPlayer
    from wallblazer.utils import get_vlc_hwdec_profile, probe_media_info

logger = logging.getLogger(LOGGER_NAME)

Gst.init(None)


_GPU_DECODER_FACTORIES = {
    "h264": "nvh264dec",
    "hevc": "nvh265dec",
    "vp9": "nvvp9dec",
}
_GPU_FILTER_ELEMENTS = ("gtkglsink", "glupload", "glcolorconvert", "glcolorbalance", "glshader")
_GPU_GAMMA_FRAGMENT = """
uniform sampler2D tex;
uniform float gamma_value;
varying vec2 v_texcoord;

void main () {
    vec4 rgba = texture2D(tex, v_texcoord);
    float safe_gamma = max(gamma_value, 0.01);
    rgba.rgb = pow(max(rgba.rgb, vec3(0.0)), vec3(1.0 / safe_gamma));
    gl_FragColor = rgba;
}
"""


def _gpu_decoder_for_source(source):
    """Return the NVIDIA decoder factory for a locally supported video source."""
    gpu_setting = str(os.environ.get("WALLBLAZER_GPU_FILTERS", "auto")).strip().lower()
    if gpu_setting in {"0", "false", "off", "no"}:
        return None
    if str(os.environ.get("WALLBLAZER_FORCE_HWDEC", "")).strip().lower() == "none":
        return None
    if not isinstance(source, str) or not os.path.isfile(source):
        return None

    gpu_profile = get_vlc_hwdec_profile()
    if gpu_profile.get("hwdec") != "cuda":
        return None

    codec = str(probe_media_info(source).get("codec") or "").strip().lower()
    decoder_name = _GPU_DECODER_FACTORIES.get(codec)
    if not decoder_name:
        return None

    decoder = Gst.ElementFactory.find(decoder_name)
    if decoder is None or decoder.get_rank() <= Gst.Rank.NONE:
        return None
    if any(Gst.ElementFactory.find(name) is None for name in _GPU_FILTER_ELEMENTS):
        return None

    # All existing video adjustments, including gamma, remain in the video
    # stream.  The GPU path handles gamma with _GPU_GAMMA_FRAGMENT below.
    return decoder_name


def _build_gpu_filter_bin():
    """Build an all-GL color path for NVIDIA-decoded video frames."""
    upload = Gst.ElementFactory.make("glupload", None)
    convert = Gst.ElementFactory.make("glcolorconvert", None)
    balance = Gst.ElementFactory.make("glcolorbalance", None)
    gamma = Gst.ElementFactory.make("glshader", None)
    if any(element is None for element in (upload, convert, balance, gamma)):
        return None, None, None

    try:
        gamma.set_property("fragment", _GPU_GAMMA_FRAGMENT)
        uniforms = Gst.Structure.new_empty("uniforms")
        uniforms.set_value("gamma_value", 1.0)
        gamma.set_property("uniforms", uniforms)
    except Exception:
        return None, None, None

    filter_bin = Gst.Bin.new("wallblazer-gpu-video-filter")
    for element in (upload, convert, balance, gamma):
        filter_bin.add(element)
    if not upload.link(convert) or not convert.link(balance) or not balance.link(gamma):
        return None, None, None

    sink_pad = upload.get_static_pad("sink")
    src_pad = gamma.get_static_pad("src")
    if sink_pad is None or src_pad is None:
        return None, None, None
    filter_bin.add_pad(Gst.GhostPad.new("sink", sink_pad))
    filter_bin.add_pad(Gst.GhostPad.new("src", src_pad))
    return filter_bin, balance, gamma


class Fade:
    def __init__(self):
        self.timer = None
        self.is_active = False

    def start(self, cur, target, step, fade_interval, update_callback: callable = None,
              complete_callback: callable = None):
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
            import threading
            self.timer = threading.Timer(
                fade_interval,
                self._fade_step,
                args=[new_cur, target, step, fade_interval, update_callback, complete_callback],
            )
            self.timer.daemon = True
            self.timer.start()

    def cancel(self):
        self.is_active = False
        if self.timer:
            self.timer.cancel()
            self.timer = None


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


class GstMedia:
    def __init__(self, source):
        self.source = source
        self.loop_video = False
        self.disable_audio = False

    def add_option(self, option):
        if not isinstance(option, str):
            return
        if option.startswith("input-repeat="):
            self.loop_video = True
        elif option == "no-audio":
            self.disable_audio = True


class GstPlayerWindow(Gtk.ApplicationWindow):
    def __init__(self, name, width, height, *args, **kwargs):
        gpu_decoder = kwargs.pop("gpu_decoder", None)
        super().__init__(*args, **kwargs)
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.name = name

        self._using_gpu_filters = False
        self._gpu_filter = None
        self._gpu_gamma_shader = None
        self._video_balance = None
        self._gamma_element = None

        if gpu_decoder:
            gpu_sink = Gst.ElementFactory.make("gtkglsink", None)
            gpu_filter, gpu_balance, gpu_gamma = _build_gpu_filter_bin()
            if gpu_sink is not None and gpu_filter is not None:
                self._sink = gpu_sink
                self._gpu_filter = gpu_filter
                self._video_balance = gpu_balance
                self._gpu_gamma_shader = gpu_gamma
                self._using_gpu_filters = True
                logger.info(
                    f"[Gst] GPU path enabled: decoder={gpu_decoder} "
                    "NVDEC -> GL color/gamma -> gtkglsink"
                )

        if not self._using_gpu_filters:
            self._sink = Gst.ElementFactory.make("gtksink")
        if self._sink is None:
            raise RuntimeError("gtksink is unavailable")
        try:
            self._sink.set_property("force-aspect-ratio", False)
        except Exception:
            pass
        self._video_widget = self._sink.get_property("widget")
        if self._video_widget is None:
            raise RuntimeError("gtksink widget is unavailable (no display?)")
        # Fill the monitor until centercrop computes the real crop geometry.
        self._video_widget.set_hexpand(True)
        self._video_widget.set_vexpand(True)
        # Set an explicit size so Gtk.Fixed shows the widget immediately.
        self._video_widget.set_size_request(self.width, self.height)

        if not self._using_gpu_filters:
            self._video_balance = Gst.ElementFactory.make("videobalance")
            self._gamma_element = Gst.ElementFactory.make("gamma")
        self._player = Gst.ElementFactory.make("playbin")
        if self._player is None:
            raise RuntimeError("playbin is unavailable")
        self._player.set_property("video-sink", self._sink)
        if self._using_gpu_filters:
            self._player.set_property("video-filter", self._gpu_filter)
        # Chain videobalance -> gamma inside a bin so all colour/gamma adjustments
        # are applied purely to the video stream.  This prevents any path where
        # gamma would fall through to the X11 display gamma ramp (XF86VidMode).
        elif self._video_balance is not None and self._gamma_element is not None:
            filter_bin = Gst.Bin.new("wallblazer-video-filter")
            filter_bin.add(self._video_balance)
            filter_bin.add(self._gamma_element)
            self._video_balance.link(self._gamma_element)
            # Ghost pads so playbin sees a single element
            sink_pad = self._video_balance.get_static_pad("sink")
            src_pad = self._gamma_element.get_static_pad("src")
            filter_bin.add_pad(Gst.GhostPad.new("sink", sink_pad))
            filter_bin.add_pad(Gst.GhostPad.new("src", src_pad))
            self._player.set_property("video-filter", filter_bin)
        elif self._video_balance is not None:
            self._player.set_property("video-filter", self._video_balance)

        self._viewport = Gtk.Fixed()
        self._viewport.set_size_request(self.width, self.height)
        self._viewport.put(self._video_widget, 0, 0)

        self._overlay = Gtk.Overlay()
        self._overlay.add(self._viewport)
        self._color_overlay = ColorTintOverlay()
        self._overlay.add_overlay(self._color_overlay)
        self._overlay.set_overlay_pass_through(self._color_overlay, True)
        self.add(self._overlay)
        self._overlay.show_all()

        self._set_black_background()

        self.fade = Fade()
        self._queued_media = None
        self._queued_source = None
        self._queued_base = None
        self._queued_phase = "forward"
        self._queued_dimensions = (None, None)
        self._active_media = None
        self._fit_mode = "cover"
        self._playback_rate = 1.0
        self._rate_applied = False
        self._rate_apply_source_id = None
        self._adjust_apply_source_id = None
        self._last_dimensions = (None, None)
        self._mute_requested = False
        self._video_adjustments = dict(DEFAULT_VIDEO_ADJUSTMENTS)
        self._last_gamma_value = DEFAULT_VIDEO_ADJUSTMENTS["gamma"]

        self.menu = None
        self.connect("button-press-event", self._on_button_press_event)
        self.connect("configure-event", self._on_configure_event)

        self._bus = self._player.get_bus()
        if self._bus is not None:
            self._bus.add_signal_watch()
            self._bus.connect("message", self._on_bus_message)

    def _set_black_background(self):
        rgba = Gdk.RGBA()
        rgba.parse("#000000")
        for widget in (self, self._overlay, self._viewport):
            try:
                widget.override_background_color(Gtk.StateFlags.NORMAL, rgba)
            except Exception:
                pass

    @staticmethod
    def _source_to_uri(source):
        if not isinstance(source, str):
            return source
        if source.startswith(("http://", "https://", "file://")):
            return source
        return pathlib.Path(source).resolve().as_uri()

    @staticmethod
    def _clamp(value, lower, upper):
        return max(lower, min(upper, value))

    def update_monitor_geometry(self, x, y, width, height):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._viewport.set_size_request(self.width, self.height)
        self._video_widget.set_size_request(self.width, self.height)
        self.resize(self.width, self.height)
        self.move(int(x), int(y))
        self.schedule_centercrop(*self._last_dimensions)

    def _on_configure_event(self, _widget, event):
        width = max(1, int(getattr(event, "width", self.width)))
        height = max(1, int(getattr(event, "height", self.height)))
        if width != self.width or height != self.height:
            self.width = width
            self.height = height
            self._viewport.set_size_request(width, height)
            self.schedule_centercrop(*self._last_dimensions)
        return False

    def _query_state(self):
        try:
            _ret, state, _pending = self._player.get_state(0)
            return state
        except Exception:
            return Gst.State.NULL

    def _query_position_ns(self):
        try:
            ok, position = self._player.query_position(Gst.Format.TIME)
            if ok:
                return max(0, int(position))
        except Exception:
            pass
        return 0

    def _query_duration_ns(self):
        try:
            ok, duration = self._player.query_duration(Gst.Format.TIME)
            if ok and duration > 0:
                return int(duration)
        except Exception:
            pass
        return 0

    def _apply_current_audio_flags(self):
        effective_mute = bool(self._mute_requested)
        if self._active_media is not None and self._active_media.disable_audio:
            effective_mute = True
        try:
            self._player.set_property("mute", effective_mute)
        except Exception as e:
            logger.debug(f"[Gst] Could not apply mute={effective_mute}: {e}")

    def set_fit_mode(self, fit_mode):
        self._fit_mode = normalize_fit_mode(fit_mode)
        self.schedule_centercrop(*self._last_dimensions)

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
        if self._active_media is None:
            return False
        state = self._query_state()
        if state not in {Gst.State.PAUSED, Gst.State.PLAYING}:
            return False
        position = self._query_position_ns()
        try:
            self._player.seek(
                self._playback_rate,
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                Gst.SeekType.SET,
                position,
                Gst.SeekType.NONE,
                Gst.CLOCK_TIME_NONE,
            )
            self._rate_applied = True
        except Exception as e:
            logger.debug(f"[Gst] Could not apply playback rate={self._playback_rate}: {e}")
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

        if self._video_balance is not None:
            try:
                self._video_balance.set_property(
                    "brightness", self._clamp(values["brightness"] - 1.0, -1.0, 1.0)
                )
                self._video_balance.set_property(
                    "contrast", self._clamp(values["contrast"], 0.0, 2.0)
                )
                self._video_balance.set_property(
                    "saturation", self._clamp(values["saturation"], 0.0, 2.0)
                )
                self._video_balance.set_property(
                    "hue", self._clamp(values["hue"] / 180.0, -1.0, 1.0)
                )
            except Exception as e:
                logger.debug(f"[Gst] Could not apply videobalance adjustments: {e}")

        # Apply gamma inside the video pipeline.  The GPU path uses a small
        # fragment shader so it never downloads frames for this adjustment.
        if self._gpu_gamma_shader is not None:
            try:
                gst_gamma = self._clamp(float(values.get("gamma", 1.0)), 0.01, 10.0)
                uniforms = Gst.Structure.new_empty("uniforms")
                uniforms.set_value("gamma_value", gst_gamma)
                self._gpu_gamma_shader.set_property("uniforms", uniforms)
                self._last_gamma_value = gst_gamma
            except Exception as e:
                logger.debug(f"[Gst] Could not apply GPU gamma adjustment: {e}")
        elif self._gamma_element is not None:
            try:
                # GStreamer gamma element range: 0.01 .. 10.0  (1.0 = neutral)
                gst_gamma = self._clamp(float(values.get("gamma", 1.0)), 0.01, 10.0)
                self._gamma_element.set_property("gamma", gst_gamma)
                self._last_gamma_value = gst_gamma
            except Exception as e:
                logger.debug(f"[Gst] Could not apply gamma adjustment: {e}")

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
        if self._active_media is None:
            return
        self._player.set_state(Gst.State.PLAYING)
        self._schedule_rate_apply()
        self._schedule_adjustment_reapply()

    def play_fade(self, target, fade_duration_sec, fade_interval):
        self.play()
        cur = 0
        step = (target - cur) / (fade_duration_sec / fade_interval)
        self.fade.cancel()
        self.fade.start(cur=cur, target=target, step=step,
                        fade_interval=fade_interval, update_callback=self.set_volume)

    def is_playing(self):
        return self._query_state() == Gst.State.PLAYING

    def pause(self):
        if self._active_media is None:
            return
        self._player.set_state(Gst.State.PAUSED)

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

    def media_new(self, source):
        return GstMedia(source)

    def set_media(self, media):
        if media is None:
            return
        self._active_media = media
        self._rate_applied = False
        self._player.set_state(Gst.State.NULL)
        self._player.set_property("uri", self._source_to_uri(media.source))
        self._apply_current_audio_flags()

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
        self.set_media(media)
        self.schedule_centercrop(video_width, video_height)
        if should_play:
            self.play()
        logger.info(
            f"[Playlist] Switched media source={source} base={base_source} phase={phase}"
        )
        return source, base_source, phase

    def set_volume(self, volume):
        try:
            volume = int(volume)
        except (TypeError, ValueError):
            volume = 0
        volume = max(0, min(100, volume))
        self._player.set_property("volume", volume / 100.0)

    def get_volume(self):
        try:
            return int(round(float(self._player.get_property("volume")) * 100))
        except Exception:
            return 0

    def set_mute(self, is_mute):
        self._mute_requested = bool(is_mute)
        self._apply_current_audio_flags()

    def get_position(self):
        duration = self._query_duration_ns()
        position = self._query_position_ns()
        if duration <= 0:
            return 0.0
        return max(0.0, min(1.0, position / duration))

    def set_position(self, position):
        duration = self._query_duration_ns()
        if duration <= 0:
            return
        try:
            position = float(position)
        except (TypeError, ValueError):
            position = 0.0
        target = int(max(0.0, min(1.0, position)) * duration)
        try:
            self._player.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                target,
            )
        except Exception as e:
            logger.debug(f"[Gst] Could not seek to {position}: {e}")

    def get_time(self):
        return int(self._query_position_ns() / 1_000_000)

    def get_length(self):
        return int(self._query_duration_ns() / 1_000_000)

    def snapshot(self, *args):
        return False

    def centercrop(self, video_width=None, video_height=None):
        if video_width is None or video_height is None:
            video_width, video_height = self._last_dimensions
        if not video_width or not video_height:
            # Dimensions not yet known — show the widget at full monitor size
            # so the window is not black while we wait for the video probe.
            try:
                self._video_widget.set_size_request(self.width, self.height)
                self._viewport.move(self._video_widget, 0, 0)
            except Exception:
                pass
            return False

        self._last_dimensions = (video_width, video_height)
        geometry = calculate_fit_geometry(
            self.width,
            self.height,
            video_width,
            video_height,
            self._fit_mode,
        )
        self._video_widget.set_size_request(geometry.width, geometry.height)
        try:
            self._viewport.move(self._video_widget, geometry.x, geometry.y)
        except Exception:
            return False
        return True

    def schedule_centercrop(self, video_width=None, video_height=None, attempts=1, delay_ms=0):
        if video_width is not None and video_height is not None:
            self._last_dimensions = (video_width, video_height)
        GLib.idle_add(self.centercrop, video_width, video_height)

    def add_audio_track(self, audio):
        logger.debug(f"[Gst] add_audio_track is not used for local video playback: {audio}")

    def _on_button_press_event(self, widget, event):
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            if not self.menu:
                self.menu = build_menu(MODE_VIDEO)
            self.menu.popup_at_pointer()
            return True
        return False

    def _notify_end_reached(self):
        app = self.get_application()
        if app is None or not hasattr(app, "on_window_end_reached"):
            return False
        GLib.idle_add(app.on_window_end_reached, self.name)
        return True

    def _on_bus_message(self, _bus, message):
        msg_type = message.type
        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"[Gst] Playback error: {err} debug={debug}")
        elif msg_type == Gst.MessageType.EOS:
            if self._active_media is not None and self._active_media.loop_video and self._queued_media is None:
                try:
                    self._player.seek_simple(
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                        0,
                    )
                    self._player.set_state(Gst.State.PLAYING)
                except Exception as e:
                    logger.warning(f"[Gst] Could not loop playback: {e}")
            elif self._notify_end_reached():
                logger.debug(f"[Gst] End reached for {self.name}; playlist transition queued")
            else:
                self._player.set_state(Gst.State.PAUSED)
        elif (
            msg_type == Gst.MessageType.STATE_CHANGED
            and message.src == self._player
            and self._query_state() == Gst.State.PLAYING
        ):
            if not self._rate_applied:
                self._schedule_rate_apply(delay_ms=50)

    def get_name(self):
        return self.name

    def cleanup(self):
        self.fade.cancel()
        self.clear_queued_media()
        self._active_media = None
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
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
        try:
            self._player.set_state(Gst.State.NULL)
        except Exception:
            pass

    def media_event_manager(self):
        return None


class GstVideoPlayer(VideoPlayer):
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

    def _gpu_decoder_for_monitor(self, gdk_monitor):
        data_source = self.config.get(CONFIG_KEY_DATA_SOURCE, {})
        if not isinstance(data_source, dict):
            return None
        source = data_source.get(gdk_monitor.get_model(), "")
        if not isinstance(source, str) or not source:
            source = data_source.get("Default", "")
        return _gpu_decoder_for_source(source)

    def new_window(self, gdk_monitor):
        rect = gdk_monitor.get_geometry()
        return GstPlayerWindow(
            gdk_monitor.get_model(),
            rect.width,
            rect.height,
            application=self,
            gpu_decoder=self._gpu_decoder_for_monitor(gdk_monitor),
        )


def main():
    logging.basicConfig(level=logging.INFO)
    app = GstVideoPlayer()
    try:
        service_handle = publish_service(DBUS_NAME_PLAYER, app)
    except Exception as e:
        logger.error(e)
        service_handle = None
    app.run(sys.argv)


if __name__ == "__main__":
    main()
