import logging
import os
import random
import signal
import subprocess
import sys
import time
import threading
import multiprocessing as mp
from multiprocessing import Process
import setproctitle

from gi.repository import GLib
from pydbus import SessionBus

try:
    from commons import *
    from player.video_player import main as video_player_main
    from player.web_player import main as web_player_main
    from gui.control import main as gui_main
    from menu import show_systray_icon
    from monitor import *
    from utils import ConfigUtil, EndSessionHandler, get_video_paths, run_runtime_self_repair
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import *
    from wallblazer.player.video_player import main as video_player_main
    from wallblazer.player.web_player import main as web_player_main
    from wallblazer.gui.control import main as gui_main
    from wallblazer.menu import show_systray_icon
    from wallblazer.utils import ConfigUtil, EndSessionHandler, get_video_paths, run_runtime_self_repair
    from wallblazer.monitor import *

loop = GLib.MainLoop()
logger = logging.getLogger(LOGGER_NAME)


class WallBlazerServer(object):
    """
    <node>
    <interface name='io.github.wallblazer.wallblazer.server'>
        <method name='null'/>
        <method name='video'>
            <arg type='s' name='video_path' direction='in'/>
            <arg type='s' name='monitor' direction='in'/>
        </method>
        <method name='stream'>
            <arg type='s' name='stream_url' direction='in'/>
        </method>
        <method name='webpage'>
            <arg type='s' name='webpage_url' direction='in'/>
        </method>
        <method name='pause_playback'/>
        <method name='start_playback'/>
        <method name="reload"/>
        <method name="playlist_next"/>
        <method name="feeling_lucky"/>
        <method name='show_gui'/>
        <method name='quit'/>
        <property name="mode" type="s" access="read"/>
        <property name="volume" type="i" access="readwrite"/>
        <property name="blur_radius" type="i" access="readwrite"/>
        <property name="is_mute" type="b" access="readwrite"/>
        <property name="is_playing" type="b" access="read"/>
        <property name="is_paused_by_user" type="b" access="readwrite"/>
        <property name="is_static_wallpaper" type="b" access="readwrite"/>
        <property name="is_pause_when_maximized" type="b" access="readwrite"/>
        <property name="is_mute_when_maximized" type="b" access="readwrite"/>
    </interface>
    </node>
    """

    def __init__(self, version, pkgdatadir, localedir, args):
        setproctitle.setproctitle("wallblazer-server")

        self.version = version
        self.pkgdatadir = pkgdatadir
        self.localedir = localedir
        self.args = args
        self._prev_mode = None
        self._player_count = 0

        # Processes
        # Use forkserver for player/systray subprocesses (they don't use GTK).
        # The GUI is launched via subprocess.Popen so GTK3 gets a clean process.
        mp.set_start_method("forkserver")
        self.gui_process = None
        self.sys_icon_process = None
        self.player_process = None

        signal.signal(signal.SIGINT, lambda *_: self.quit())
        signal.signal(signal.SIGTERM, lambda *_: self.quit())
        # SIGSEGV as a fail-safe
        signal.signal(signal.SIGSEGV, lambda *_: self.quit())
        # Monitoring EndSession (OS reboot, shutdown, etc.)
        EndSessionHandler(self.quit)

        # Configuration
        if args.reset:
            ConfigUtil().generate_template()
        repair_status = run_runtime_self_repair()
        if repair_status.get("missing_binaries"):
            logger.warning(
                "[Server] Runtime dependencies missing: "
                + ", ".join(repair_status["missing_binaries"])
            )
        self._load_config()

        # Playlist timer
        self._playlist_timer_id = None
        self._playlist_indices = {}  # {monitor_name: current_index}
        self._player_watchdog_id = None
        self._last_player_restart_ts = 0.0

        # Player process
        self.reload()
        self._player_watchdog_id = GLib.timeout_add_seconds(6, self._player_watchdog_tick)

        # Show main GUI
        if not args.background:
            self.show_gui()

        logger.info("[Server] Started")

    def _load_config(self):
        self.config = ConfigUtil().load()

    def _save_config(self):
        ConfigUtil().save(self.config)

    def _setup_player(self, mode, data_source=None, monitor=None):
        """Setup and run player"""
        logger.info(f"[Mode] {mode}")
        self.config[CONFIG_KEY_MODE] = mode

        # Set data source if specified
        if data_source is not None and monitor:
            self.config[CONFIG_KEY_DATA_SOURCE][monitor] = data_source
        if data_source is not None:
            self.config[CONFIG_KEY_DATA_SOURCE]['Default'] = data_source
        # Persist before launching player so it always reads the latest mode/source.
        self._save_config()

        # Ask current player to quit, but don't let this block server responsiveness.
        self._quit_player(timeout_sec=0.8)

        # Terminate old player process and wait for it to finish
        if self.player_process:
            self.player_process.terminate()
            self.player_process.join(timeout=5)  # Wait up to 5 seconds
            if self.player_process.is_alive():
                logger.warning("[Server] Player process didn't terminate, killing it")
                self.player_process.kill()
                self.player_process.join(timeout=2)
            self.player_process = None

        if mode in [MODE_VIDEO, MODE_STREAM]:
            self.player_process = Process(
                name=f"wallblazer-player-{self._player_count}", target=video_player_main)
        elif mode == MODE_WEBPAGE:
            self.player_process = Process(
                name=f"wallblazer-player-{self._player_count}", target=web_player_main)
        elif mode == MODE_NULL:
            pass
        else:
            raise ValueError("[Server] Unknown mode")
        if self.player_process is not None:
            self.player_process.start()
            self._player_count += 1

        # Refresh systray icon if the mode changed
        if self.config[CONFIG_KEY_SYSTRAY]:
            if self._prev_mode != self.mode:
                if self.sys_icon_process:
                    self.sys_icon_process.terminate()
                    self.sys_icon_process.join(timeout=3)
                    if self.sys_icon_process.is_alive():
                        self.sys_icon_process.kill()
                        self.sys_icon_process.join(timeout=1)
                self.sys_icon_process = Process(
                    name="wallblazer-systray", target=show_systray_icon, args=(mode,))
                self.sys_icon_process.start()
            self._prev_mode = self.mode

    def _player_watchdog_tick(self):
        """
        Auto-repair loop: if player process exits unexpectedly, restart it.
        Keeps running as long as server main loop is alive.
        """
        if self.mode == MODE_NULL:
            return True

        proc = self.player_process
        if proc is not None and proc.is_alive():
            return True

        now = time.time()
        if now - self._last_player_restart_ts < 3.0:
            return True
        self._last_player_restart_ts = now

        exit_code = None if proc is None else proc.exitcode
        logger.warning(f"[Watchdog] Player process is down (exit={exit_code}). Restarting...")
        try:
            self._load_config()
            mode = self.config.get(CONFIG_KEY_MODE, MODE_VIDEO)
            self._setup_player(mode)
            logger.info("[Watchdog] Player auto-repair restart succeeded")
        except Exception as e:
            logger.error(f"[Watchdog] Player auto-repair failed: {e}")
        return True

    @staticmethod
    def _quit_player(timeout_sec=0.8):
        """Request current player to quit, without blocking forever."""
        player = get_instance(DBUS_NAME_PLAYER)
        if not player:
            return True

        done = threading.Event()

        def _worker():
            try:
                player.quit_player()
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        if not done.wait(timeout_sec):
            logger.warning("[Server] Timed out waiting for player.quit_player()")
            return False
        return True

    def video(self, video_path=None, monitor=None):
        self._setup_player(MODE_VIDEO, video_path, monitor)

    def stream(self, stream_url=None):
        self._setup_player(MODE_STREAM, stream_url)

    def webpage(self, webpage_url=None):
        self._setup_player(MODE_WEBPAGE, webpage_url)

    @staticmethod
    def pause_playback():
        player = get_instance(DBUS_NAME_PLAYER)
        if player:
            player.pause_playback()

    @staticmethod
    def start_playback():
        player = get_instance(DBUS_NAME_PLAYER)
        if player:
            player.start_playback()

    def reload(self):
        # GUI writes config to disk directly; always refresh before rebuilding players.
        self._load_config()
        if self.config[CONFIG_KEY_MODE] == MODE_VIDEO:
            self.video()
        elif self.config[CONFIG_KEY_MODE] == MODE_STREAM:
            self.stream()
        elif self.config[CONFIG_KEY_MODE] == MODE_WEBPAGE:
            self.webpage()
        elif self.config[CONFIG_KEY_MODE] == MODE_NULL:
            pass
        else:
            raise ValueError("[Server] Unknown mode")
        self._restart_playlist_timer()

    def playlist_next(self):
        """Advance playlist by one item when interval=0 mode is active."""
        self._load_config()
        if not self.config.get(CONFIG_KEY_PLAYLIST, False):
            return
        interval = int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300))
        if interval != 0:
            return
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            try:
                player.playlist_next()
                return
            except Exception as e:
                logger.warning(f"[Playlist] In-player next failed: {e}")
        self._on_playlist_tick()

    def _restart_playlist_timer(self):
        """Cancel any existing playlist timer and restart if playlist is enabled."""
        if self._playlist_timer_id is not None:
            GLib.source_remove(self._playlist_timer_id)
            self._playlist_timer_id = None
        if not self.config.get(CONFIG_KEY_PLAYLIST, False):
            return
        interval = int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300))
        # Initialize per-monitor indices
        monitor_names = [
            name for name in self.config.get(CONFIG_KEY_DATA_SOURCE, {}).keys()
            if isinstance(name, str)
        ]
        for monitor_name in monitor_names:
            videos = self._get_monitor_playlist_videos(monitor_name)
            if not videos:
                continue
            current_src = self.config.get(CONFIG_KEY_DATA_SOURCE, {}).get(monitor_name, "")
            if current_src in videos:
                self._playlist_indices[monitor_name] = videos.index(current_src)
            else:
                self._playlist_indices[monitor_name] = -1
        if interval <= 0:
            logger.info("[Playlist] Timer disabled (interval=0). Player handles seamless preloaded transitions.")
            return
        self._playlist_timer_id = GLib.timeout_add_seconds(interval, self._on_playlist_tick)
        logger.info(f"[Playlist] Timer started, interval={interval}s")

    def _get_playlist_candidates(self):
        """Return the ordered list of videos used by playlist mode."""
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        active_name = self.config.get(CONFIG_KEY_PLAYLIST_ACTIVE)
        if isinstance(library, dict) and isinstance(active_name, str):
            active_list = library.get(active_name, [])
            if isinstance(active_list, list):
                existing = [
                    video
                    for video in active_list
                    if isinstance(video, str) and os.path.isfile(video)
                ]
                if existing:
                    return existing

        selected = self.config.get(CONFIG_KEY_PLAYLIST_SELECTION, [])
        if isinstance(selected, list):
            existing = [video for video in selected if isinstance(video, str) and os.path.isfile(video)]
            if existing:
                return existing
        return get_video_paths()

    def _get_monitor_playlist_videos(self, monitor_name):
        library = self.config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
        assignments = self.config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if isinstance(library, dict) and isinstance(assignments, dict):
            playlist_name = assignments.get(monitor_name)
            if isinstance(playlist_name, str):
                playlist_items = library.get(playlist_name, [])
                if isinstance(playlist_items, list):
                    return [
                        video for video in playlist_items
                        if isinstance(video, str) and os.path.isfile(video)
                    ]
        monitor_playlists = self.config.get(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        if isinstance(monitor_playlists, dict):
            legacy_items = monitor_playlists.get(monitor_name, [])
            if isinstance(legacy_items, list):
                return [
                    video for video in legacy_items
                    if isinstance(video, str) and os.path.isfile(video)
                ]
        return []

    def _on_playlist_tick(self):
        """Called by GLib timer to advance each monitor to the next video in its playlist."""
        self._load_config()
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None and self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
            try:
                player.playlist_next()
                return True
            except Exception as e:
                logger.warning(f"[Playlist] In-player tick failed, falling back to server logic: {e}")

        shuffle = self.config.get(CONFIG_KEY_PLAYLIST_SHUFFLE, False)
        paths = self.config.get(CONFIG_KEY_DATA_SOURCE, {})
        changed = False

        monitor_names = [name for name in paths.keys() if isinstance(name, str)]
        for monitor_name in monitor_names:
            existing = self._get_monitor_playlist_videos(monitor_name)
            if not existing:
                continue
            if shuffle:
                next_video = random.choice(existing)
            else:
                idx = self._playlist_indices.get(monitor_name, -1)
                idx = (idx + 1) % len(existing)
                self._playlist_indices[monitor_name] = idx
                next_video = existing[idx]
            logger.info("[Playlist] Advanced to next item for one monitor.")
            paths[monitor_name] = next_video
            changed = True

        if changed:
            self.config[CONFIG_KEY_MODE] = MODE_VIDEO
            self.config[CONFIG_KEY_DATA_SOURCE] = paths
            ConfigUtil().save(self.config)
            if player is not None and self.mode == MODE_VIDEO:
                try:
                    player.apply_video_config()
                except Exception as e:
                    logger.warning(f"[Playlist] In-process apply failed, restarting player: {e}")
                    self._setup_player(MODE_VIDEO)
            else:
                self._setup_player(MODE_VIDEO)
        return True  # keep timer running

    def feeling_lucky(self):
        """Random play a video from the directory"""
        monitors = Monitors().get_monitors()
        for monitor in monitors:
            file_list = get_video_paths()   
            # Remove current data source from the random selection
            if self.config[CONFIG_KEY_DATA_SOURCE][monitor] in file_list:
                file_list.remove(self.config[CONFIG_KEY_DATA_SOURCE][monitor])
            if file_list:
                video_path = random.choice(file_list)
                self.config[CONFIG_KEY_MODE] = MODE_VIDEO
                self.config[CONFIG_KEY_DATA_SOURCE][monitor] = video_path
                self._save_config()
            self.video(video_path)

    def show_gui(self):
        """Show main GUI in a completely fresh subprocess to avoid GTK+fork segfaults.
        Use the installed wallblazer launcher directly so PYTHONPATH is already correct.
        """
        # Kill existing GUI if still running
        if self.gui_process is not None:
            try:
                if self.gui_process.poll() is None:  # still running
                    self.gui_process.terminate()
                    try:
                        self.gui_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.gui_process.kill()
            except Exception:
                pass

        # Use the installed wallblazer launcher which already sets PYTHONPATH correctly.
        # Pass --gui-only so it skips server logic and only opens the GTK window.
        import shutil
        launcher = shutil.which("wallblazer") or "/usr/bin/wallblazer"
        cmd = [launcher, "--gui-only", self.pkgdatadir, self.localedir]
        env = __import__("os").environ.copy()
        env["GDK_BACKEND"] = "x11"
        env["VLC_VERBOSE"] = "-1"
        # Ensure the package dir is also on PYTHONPATH for the GUI subprocess
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (self.pkgdatadir + ":" + existing_pp).rstrip(":")
        self.gui_process = subprocess.Popen(cmd, env=env)

    def quit(self):
        if self._player_watchdog_id is not None:
            try:
                GLib.source_remove(self._player_watchdog_id)
            except Exception:
                pass
            self._player_watchdog_id = None

        try:
            self._quit_player(timeout_sec=0.8)
        except GLib.Error:
            pass
        
        # Quit all processes with proper cleanup
        for process in [self.player_process, self.sys_icon_process]:
            if process and process.is_alive():
                process.terminate()
                process.join(timeout=3)
                if process.is_alive():
                    logger.warning(f"[Server] Process {process.name} didn't terminate, killing it")
                    process.kill()
                    process.join(timeout=1)

        # GUI process is a subprocess.Popen, not multiprocessing.Process
        if self.gui_process is not None:
            try:
                if self.gui_process.poll() is None:
                    self.gui_process.terminate()
                    try:
                        self.gui_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.gui_process.kill()
            except Exception:
                pass
        
        loop.quit()
        logger.info("[Server] Stopped")

    @property
    def mode(self):
        return self.config[CONFIG_KEY_MODE]

    @property
    def volume(self):
        return self.config[CONFIG_KEY_VOLUME]

    @volume.setter
    def volume(self, volume):
        self.config[CONFIG_KEY_VOLUME] = volume
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            player.volume = volume

    @property
    def blur_radius(self):
        return self.config[CONFIG_KEY_BLUR_RADIUS]

    @blur_radius.setter
    def blur_radius(self, blur_radius):
        self.config[CONFIG_KEY_BLUR_RADIUS] = blur_radius
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            player.reload_config()

    @property
    def is_mute(self):
        return self.config[CONFIG_KEY_MUTE]

    @is_mute.setter
    def is_mute(self, is_mute):
        self.config[CONFIG_KEY_MUTE] = is_mute
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            player.is_mute = is_mute

    @property
    def is_playing(self):
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            return player.is_playing
        return False

    @property
    def is_paused_by_user(self):
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None and player.mode in [MODE_VIDEO, MODE_STREAM]:
            return player.is_paused_by_user
        return None

    @is_paused_by_user.setter
    def is_paused_by_user(self, is_paused_by_user):
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None and player.mode in [MODE_VIDEO, MODE_STREAM]:
            player.is_paused_by_user = is_paused_by_user

    @property
    def is_static_wallpaper(self):
        return self.config[CONFIG_KEY_STATIC_WALLPAPER]

    @is_static_wallpaper.setter
    def is_static_wallpaper(self, is_static_wallpaper):
        self.config[CONFIG_KEY_STATIC_WALLPAPER] = is_static_wallpaper
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            player.reload_config()

    @property
    def is_pause_when_maximized(self):
        return self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED]

    @is_pause_when_maximized.setter
    def is_pause_when_maximized(self, is_pause_when_maximized):
        self.config[CONFIG_KEY_PAUSE_WHEN_MAXIMIZED] = is_pause_when_maximized
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            player.reload_config()

    @property
    def is_mute_when_maximized(self):
        return self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED]

    @is_mute_when_maximized.setter
    def is_mute_when_maximized(self, is_mute_when_maximized):
        self.config[CONFIG_KEY_MUTE_WHEN_MAXIMIZED] = is_mute_when_maximized
        player = get_instance(DBUS_NAME_PLAYER)
        if player is not None:
            player.reload_config()


def get_instance(dbus_name):
    bus = SessionBus()
    try:
        instance = bus.get(dbus_name)
    except GLib.Error:
        return None
    return instance


def _call_with_timeout(func, timeout_sec=2.0):
    result = {"ok": False, "err": None}
    done = threading.Event()

    def _runner():
        try:
            func()
            result["ok"] = True
        except Exception as e:
            result["err"] = e
        finally:
            done.set()

    threading.Thread(target=_runner, daemon=True).start()
    if not done.wait(timeout_sec):
        return False
    if result["err"] is not None:
        raise result["err"]
    return result["ok"]


def _kill_stale_servers():
    """Kill any old wallblazer-server processes that may be stuck on the bus."""
    try:
        subprocess.run(["pkill", "-f", "wallblazer-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "wallblazer-player"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "wallblazer --gui-only"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time as _time
        _time.sleep(0.8)  # brief pause so DBus names are released
    except Exception:
        pass


def main(version, pkgdatadir, localedir, args):
    existing = get_instance(DBUS_NAME_SERVER)
    if existing is not None:
        # Try to tell the running server to pop up its GUI window
        try:
            ok = _call_with_timeout(lambda: existing.show_gui(), timeout_sec=2.0)
            if ok:
                return
            # Timed out waiting for DBus reply from existing server.
            logger.warning("[Server] Existing server call timed out, restarting...")
            _kill_stale_servers()
        except Exception:
            # The existing server is stale/broken — kill it and start fresh
            logger.warning("[Server] Existing server is unresponsive, restarting...")
            _kill_stale_servers()
    elif args.background:
        # In background mode, avoid orphan stacks from stale previous sessions.
        _kill_stale_servers()

    # Pause before launching (used for autostart delay)
    time.sleep(args.p)

    bus = SessionBus()
    server = WallBlazerServer(version, pkgdatadir, localedir, args)
    try:
        bus.publish(DBUS_NAME_SERVER, server)
        loop.run()
    except RuntimeError:
        raise Exception("[Server] Failed to publish DBus name – another instance may be running.")
