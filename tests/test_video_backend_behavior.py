import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))

from player.fit_geometry import calculate_fit_geometry
from player.video_player import PlayerWindow
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


class BackendSelectionTests(unittest.TestCase):
    def test_auto_uses_vlc_on_x11_when_both_backends_exist(self):
        with (
            patch.dict(
                os.environ,
                {"WALLBLAZER_VIDEO_BACKEND": "auto", "XDG_SESSION_TYPE": "x11"},
                clear=False,
            ),
            patch.object(server, "gst_video_player_available", True),
            patch.object(server, "_vlc_video_player_available", return_value=True),
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


if __name__ == "__main__":
    unittest.main()
