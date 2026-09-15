import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))

import server


class DeleteCurrentVideoTests(unittest.TestCase):
    def _server_for(self, source, extra_sources=None):
        extra_sources = extra_sources or {}
        data_source = {"Default": str(source), **extra_sources}
        app = server.WallBlazerServer.__new__(server.WallBlazerServer)
        app.config = {
            server.CONFIG_KEY_MODE: server.MODE_VIDEO,
            server.CONFIG_KEY_DATA_SOURCE: data_source,
            server.CONFIG_KEY_PLAYLIST_SELECTION: [str(source), "/tmp/keep.mp4"],
            server.CONFIG_KEY_PLAYLIST_LIBRARY: {
                "Default": [str(source), "/tmp/keep.mp4"],
            },
            server.CONFIG_KEY_MONITOR_PLAYLISTS: {
                "monitor": [str(source), "/tmp/keep.mp4"],
            },
            server.CONFIG_KEY_REVERSE_PLAYLIST_ITEMS: {
                "Default": [str(source), "/tmp/keep.mp4"],
            },
        }
        app.player_process = None
        app._load_config = Mock()
        app._save_config = Mock()
        app._setup_player = Mock()
        app._stop_player_for_delete = Mock()
        app.feeling_lucky = Mock()
        return app

    def test_deletes_only_the_confirmed_current_video_and_prunes_references(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "space name.webm"
            other = Path(directory) / "keep.mp4"
            current.write_bytes(b"current")
            other.write_bytes(b"other")
            app = self._server_for(current, {"HDMI-1": str(current)})

            with patch.object(server, "get_instance", return_value=None), patch.object(
                server, "get_video_paths", return_value=[]
            ):
                self.assertTrue(app.delete_current_video(str(current)))

            self.assertFalse(current.exists())
            self.assertTrue(other.exists())
            self.assertEqual(app.config[server.CONFIG_KEY_MODE], server.MODE_NULL)
            self.assertTrue(all(not value for value in app.config[server.CONFIG_KEY_DATA_SOURCE].values()))
            self.assertNotIn(str(current), app.config[server.CONFIG_KEY_PLAYLIST_SELECTION])
            self.assertNotIn(str(current), app.config[server.CONFIG_KEY_PLAYLIST_LIBRARY]["Default"])
            self.assertNotIn(str(current), app.config[server.CONFIG_KEY_MONITOR_PLAYLISTS]["monitor"])
            self.assertNotIn(str(current), app.config[server.CONFIG_KEY_REVERSE_PLAYLIST_ITEMS]["Default"])
            app._stop_player_for_delete.assert_called_once_with()
            app._setup_player.assert_called_with(server.MODE_NULL)

    def test_loads_another_video_after_successful_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "current.mp4"
            other = Path(directory) / "other.mkv"
            current.write_bytes(b"current")
            other.write_bytes(b"other")
            app = self._server_for(current)

            with patch.object(server, "get_instance", return_value=None), patch.object(
                server, "get_video_paths", return_value=[str(other)]
            ):
                self.assertTrue(app.delete_current_video(str(current)))

            self.assertFalse(current.exists())
            self.assertEqual(app.config[server.CONFIG_KEY_MODE], server.MODE_VIDEO)
            self.assertEqual(app.config[server.CONFIG_KEY_DATA_SOURCE]["Default"], str(other))
            app._setup_player.assert_called_with(server.MODE_VIDEO)

    def test_rejects_urls_and_missing_files_without_stopping_playback(self):
        app = self._server_for("https://example.test/wallpaper.mp4")
        with patch.object(server, "get_instance", return_value=None):
            self.assertFalse(app.delete_current_video("https://example.test/wallpaper.mp4"))
        app._stop_player_for_delete.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "gone.mp4"
            app = self._server_for(missing)
            with patch.object(server, "get_instance", return_value=None):
                self.assertFalse(app.delete_current_video(str(missing)))
            app._stop_player_for_delete.assert_not_called()

    def test_rejects_confirmation_for_a_different_file(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "current.mp4"
            different = Path(directory) / "different.mp4"
            current.write_bytes(b"current")
            different.write_bytes(b"different")
            app = self._server_for(current)
            with patch.object(server, "get_instance", return_value=None):
                self.assertFalse(app.delete_current_video(str(different)))
            self.assertTrue(current.exists())
            app._stop_player_for_delete.assert_not_called()

    def test_restores_playback_when_filesystem_delete_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "current.mp4"
            current.write_bytes(b"current")
            app = self._server_for(current)
            with patch.object(server, "get_instance", return_value=None), patch.object(
                server.os, "remove", side_effect=PermissionError("denied")
            ):
                self.assertFalse(app.delete_current_video(str(current)))
            self.assertTrue(current.exists())
            app._setup_player.assert_called_with(server.MODE_VIDEO)

    def test_does_not_delete_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "not-a-video.mp4"
            target.mkdir()
            app = self._server_for(target)
            with patch.object(server, "get_instance", return_value=None):
                self.assertFalse(app.delete_current_video(str(target)))
            self.assertTrue(target.is_dir())
            app._stop_player_for_delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
