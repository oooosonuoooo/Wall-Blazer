import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))

from player.fit_geometry import calculate_fit_geometry
from player import gst_video_player
from player.video_player import PlayerWindow, VideoPlayer
import utils
import server


class VlcCoverCropTests(unittest.TestCase):
    def test_cover_child_overflows_viewport_symmetrically(self):
        geometry = calculate_fit_geometry(1680, 1050, 1920, 1080, "cover")
        self.assertEqual((geometry.width, geometry.height), (1867, 1050))
        self.assertEqual((geometry.x, geometry.y), (-94, 0))


class VlcEndEventTests(unittest.TestCase):
    def test_end_event_is_dispatched_to_glib_main_loop(self):
        callback = object()
        window = SimpleNamespace(
            _is_disposed=False,
            _media_generation=4,
            _dispatch_media_end_reached=callback,
        )
        with patch("player.video_player.GLib.idle_add") as idle_add:
            PlayerWindow._on_media_end_reached(window, None)
        idle_add.assert_called_once_with(callback, 4)

    def test_late_end_event_cannot_skip_replaced_media(self):
        calls = []
        app = SimpleNamespace(on_window_end_reached=lambda monitor: calls.append(monitor))
        window = SimpleNamespace(
            _is_disposed=False,
            _media_generation=5,
            get_application=lambda: app,
            name="HDMI-0",
        )
        self.assertFalse(PlayerWindow._dispatch_media_end_reached(window, 4))
        self.assertEqual(calls, [])
        self.assertFalse(PlayerWindow._dispatch_media_end_reached(window, 5))
        self.assertEqual(calls, ["HDMI-0"])


class PreloadTransitionTests(unittest.TestCase):
    def test_gstreamer_queue_media_accepts_audio_policy(self):
        media = SimpleNamespace(disable_audio=False)
        window = SimpleNamespace()

        gst_video_player.GstPlayerWindow.queue_media(
            window,
            media,
            "/tmp/next.mp4",
            "/tmp/base.mp4",
            "forward",
            1920,
            1080,
            disable_audio=True,
        )

        self.assertTrue(media.disable_audio)
        self.assertIs(window._queued_media, media)
        self.assertEqual(window._queued_source, "/tmp/next.mp4")
        self.assertEqual(window._queued_base, "/tmp/base.mp4")
        self.assertEqual(window._queued_phase, "forward")
        self.assertEqual(window._queued_dimensions, (1920, 1080))

    def test_queue_media_starts_a_gpu_preload_with_audio_policy(self):
        start_preload = Mock()
        window = SimpleNamespace(_start_preload=start_preload)

        PlayerWindow.queue_media(
            window,
            "media",
            "/tmp/next.mp4",
            "/tmp/base.mp4",
            "forward",
            1920,
            1080,
            disable_audio=True,
        )

        self.assertEqual(window._queued_media, "media")
        self.assertEqual(window._queued_source, "/tmp/next.mp4")
        self.assertEqual(window._queued_base, "/tmp/base.mp4")
        self.assertEqual(window._queued_phase, "forward")
        self.assertEqual(window._queued_dimensions, (1920, 1080))
        start_preload.assert_called_once_with("/tmp/next.mp4", disable_audio=True)

    def test_prepared_layer_is_promoted_before_old_layer_stops(self):
        events = []

        class FakePlayer:
            def __init__(self, name):
                self.name = name

            def audio_get_volume(self):
                return 83

            def audio_get_mute(self):
                return 0

            def video_set_aspect_ratio(self, value):
                events.append((self.name, "aspect", value))

            def video_set_crop_geometry(self, value):
                events.append((self.name, "crop", value))

            def audio_set_volume(self, value):
                events.append((self.name, "volume", value))

            def audio_set_mute(self, value):
                events.append((self.name, "mute", value))

            def set_pause(self, value):
                events.append((self.name, "pause", value))

            def stop(self):
                events.append((self.name, "stop"))

        old_widget = SimpleNamespace(player=FakePlayer("old"))
        prepared_widget = SimpleNamespace(player=FakePlayer("prepared"))
        window = SimpleNamespace(
            _PlayerWindow__vlc_widget=old_widget,
            _PlayerWindow__preload_widget=prepared_widget,
            _active_media="old-media",
            _active_source="/tmp/old.mp4",
            _media_generation=2,
            _preload_timer_id=9,
            _preload_source="/tmp/next.mp4",
            _preload_media="next-media",
            _preload_started=True,
            _preload_ready=True,
            _preload_failed=False,
            _preload_disable_audio=False,
            _preload_started_at=1.0,
            _preload_switch_requested=True,
            _preload_switch_callback=object(),
            _apply_crop_for_widget=lambda *args: True,
            _detach_end_reached_handler=lambda: None,
            _attach_end_reached_handler=lambda: None,
            _raise_widget=lambda *args: None,
            _schedule_rate_apply=lambda *args, **kwargs: None,
            _schedule_adjustment_reapply=lambda *args, **kwargs: None,
        )

        self.assertTrue(
            PlayerWindow._activate_preloaded_media(
                window,
                "next-media",
                "/tmp/next.mp4",
                1920,
                1080,
                True,
            )
        )

        self.assertIs(window._PlayerWindow__vlc_widget, prepared_widget)
        self.assertIs(window._PlayerWindow__preload_widget, old_widget)
        self.assertEqual(window._active_source, "/tmp/next.mp4")
        self.assertEqual(window._media_generation, 3)
        self.assertLess(events.index(("prepared", "pause", 0)), events.index(("old", "stop")))


class BackendSelectionTests(unittest.TestCase):
    def test_gpu_filter_keeps_nvdec_frames_in_gl_memory(self):
        self.assertEqual(
            set(gst_video_player._GPU_FILTER_ELEMENTS),
            {"videorate", "glcolorconvert", "glcolorbalance"},
        )

    def test_gpu_presentation_rate_is_bounded_for_wallpaper_playback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gst_video_player._gpu_max_fps(), 30)
        with patch.dict(os.environ, {"WALLBLAZER_GPU_MAX_FPS": "30"}, clear=False):
            self.assertEqual(gst_video_player._gpu_max_fps(), 30)
        with patch.dict(os.environ, {"WALLBLAZER_GPU_MAX_FPS": "120"}, clear=False):
            self.assertEqual(gst_video_player._gpu_max_fps(), 60)
        with patch.dict(os.environ, {"WALLBLAZER_GPU_MAX_FPS": "bad"}, clear=False):
            self.assertEqual(gst_video_player._gpu_max_fps(), 30)

    def test_gpu_render_path_prefers_native_x11_sink(self):
        self.assertEqual(
            gst_video_player._GPU_RENDER_SINKS,
            ("glimagesink", "gtkglsink"),
        )

    def test_gpu_center_crop_idle_callback_is_one_shot(self):
        video_widget = Mock()
        viewport = Mock()
        window = SimpleNamespace(
            width=1920,
            height=1080,
            _fit_mode="cover",
            _last_dimensions=(None, None),
            _video_widget=video_widget,
            _viewport=viewport,
        )

        self.assertFalse(
            gst_video_player.GstPlayerWindow.centercrop(
                window, 2560, 1440
            )
        )
        video_widget.set_size_request.assert_called_once()
        viewport.move.assert_called_once()

    def test_auto_uses_gpu_gstreamer_on_x11_when_gpu_path_exists(self):
        with (
            patch.dict(
                os.environ,
                {"WALLBLAZER_VIDEO_BACKEND": "auto", "XDG_SESSION_TYPE": "x11"},
                clear=False,
            ),
            patch.object(server, "gst_video_player_available", True),
            patch.object(server, "_vlc_video_player_available", return_value=True),
            patch.object(server, "_gstreamer_gpu_path_available", return_value=True),
        ):
            self.assertTrue(server._prefer_gstreamer_video_backend())

    def test_auto_falls_back_to_vlc_on_x11_without_gpu_path(self):
        with (
            patch.dict(
                os.environ,
                {"WALLBLAZER_VIDEO_BACKEND": "auto", "XDG_SESSION_TYPE": "x11"},
                clear=False,
            ),
            patch.object(server, "gst_video_player_available", True),
            patch.object(server, "_vlc_video_player_available", return_value=True),
            patch.object(server, "_gstreamer_gpu_path_available", return_value=False),
        ):
            self.assertFalse(server._prefer_gstreamer_video_backend())

    def test_auto_keeps_gstreamer_on_wayland(self):
        with (
            patch.dict(
                os.environ,
                {"WALLBLAZER_VIDEO_BACKEND": "auto", "XDG_SESSION_TYPE": "wayland"},
                clear=False,
            ),
            patch.object(server, "gst_video_player_available", True),
            patch.object(server, "_vlc_video_player_available", return_value=True),
        ):
            self.assertTrue(server._prefer_gstreamer_video_backend())

    def test_new_configs_keep_wallpaper_playing_behind_maximized_windows(self):
        self.assertFalse(
            server.CONFIG_TEMPLATE[server.CONFIG_KEY_PAUSE_WHEN_MAXIMIZED]
        )

    def test_video_source_update_uses_existing_player_process(self):
        app = server.WallBlazerServer.__new__(server.WallBlazerServer)
        app.config = {
            server.CONFIG_KEY_MODE: server.MODE_VIDEO,
            server.CONFIG_KEY_DATA_SOURCE: {"Default": "/tmp/old.mp4"},
        }
        app.player_process = None
        app._save_config = Mock()
        app._quit_player = Mock()
        app._restart_playlist_timer = Mock()
        player = SimpleNamespace(
            mode=server.MODE_VIDEO,
            apply_video_config=Mock(),
        )

        with patch.object(server, "get_instance", return_value=player):
            app._setup_player(server.MODE_VIDEO, data_source="/tmp/new.mp4")

        self.assertEqual(
            app.config[server.CONFIG_KEY_DATA_SOURCE]["Default"],
            "/tmp/new.mp4",
        )
        player.apply_video_config.assert_called_once_with()
        app._restart_playlist_timer.assert_called_once_with()
        app._quit_player.assert_not_called()

    def test_single_video_end_event_recovers_the_current_layer(self):
        recover = Mock()
        trace = Mock()
        monitor = SimpleNamespace(get_model=lambda: "HDMI-0")
        window = SimpleNamespace(
            has_direct_switch_pending=lambda: False,
            keep_current_media_playing=recover,
        )
        player = SimpleNamespace(
            mode=server.MODE_VIDEO,
            _find_monitor_window=lambda name: (monitor, window),
            _reverse_state={},
            _get_source_for_monitor=lambda name, sources: "/tmp/current.mp4",
            config={server.CONFIG_KEY_DATA_SOURCE: {"Default": "/tmp/current.mp4"}},
            _needs_transition_for_monitor=lambda name, source: False,
            _trace_transition=trace,
        )

        self.assertFalse(VideoPlayer.on_window_end_reached(player, "HDMI-0"))
        recover.assert_called_once_with()
        trace.assert_called_once()
        self.assertEqual(trace.call_args.args[0], "loop_recovery")


class GpuPlaybackCacheTests(unittest.TestCase):
    def test_unsupported_codec_uses_cached_nvenc_copy_and_preserves_original(self):
        source_info = {
            "codec": "av1",
            "width": 3840,
            "height": 2160,
            "avg_frame_rate": "60/1",
        }
        cached_info = {
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "60/1",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "wallpaper.mkv"
            source.write_bytes(b"original")

            def fake_probe(path, force_refresh=False):
                return source_info if str(path) == str(source) else cached_info

            def fake_ffmpeg(args, timeout=None, low_priority=True):
                Path(args[-1]).write_bytes(b"prepared")
                return SimpleNamespace(returncode=0)

            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": temp_dir}, clear=False),
                patch.object(utils, "probe_media_info", side_effect=fake_probe),
                patch.object(utils, "_ffmpeg_run_quiet", side_effect=fake_ffmpeg) as run,
            ):
                result = utils.prepare_gpu_playback_video(str(source))
                prepared_source = utils.get_gpu_playback_source(str(source))

            self.assertTrue(result["ok"])
            self.assertTrue(result["changed"])
            self.assertEqual(result["encoder"], "h264_nvenc")
            self.assertTrue(source.is_file())
            self.assertEqual(prepared_source, result["output_path"])
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")

    def test_gst_media_keeps_original_identity_but_uses_cached_source(self):
        with patch.object(
            gst_video_player,
            "get_gpu_playback_source",
            return_value="/cache/wallpaper-h264.mp4",
        ):
            media = gst_video_player.GstMedia("/videos/wallpaper.mkv")

        self.assertEqual(media.original_source, "/videos/wallpaper.mkv")
        self.assertEqual(media.source, "/cache/wallpaper-h264.mp4")

    def test_auto_preparation_defaults_to_available_gpu_and_can_be_disabled(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(server, "_gstreamer_gpu_path_available", return_value=True),
        ):
            self.assertTrue(server._is_gpu_playback_preparation_enabled())
        with (
            patch.dict(os.environ, {"WALLBLAZER_GPU_COMPAT_PREPARE": "0"}, clear=True),
            patch.object(server, "_gstreamer_gpu_path_available", return_value=True),
        ):
            self.assertFalse(server._is_gpu_playback_preparation_enabled())

    def test_gpu_preparation_collects_all_unique_playlist_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = [Path(temp_dir) / f"wallpaper-{index}.mkv" for index in range(4)]
            for path in paths:
                path.write_bytes(b"video")
            app = server.WallBlazerServer.__new__(server.WallBlazerServer)
            app.config = {
                server.CONFIG_KEY_DATA_SOURCE: {"Default": str(paths[0])},
                server.CONFIG_KEY_PLAYLIST_SELECTION: [str(paths[0]), str(paths[1])],
                server.CONFIG_KEY_PLAYLIST_LIBRARY: {"Default": [str(paths[1]), str(paths[2])]},
                server.CONFIG_KEY_MONITOR_PLAYLISTS: {"HDMI-0": [str(paths[2]), str(paths[3])]},
            }

            sources = app._configured_video_sources_for_gpu()

        self.assertEqual(set(sources), {str(path) for path in paths})


if __name__ == "__main__":
    unittest.main()
