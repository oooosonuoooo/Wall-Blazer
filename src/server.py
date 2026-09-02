import logging
import os
import random
import signal
import shutil
import subprocess
import sys
import time
import threading
import hashlib
import multiprocessing as mp
import importlib.util
from multiprocessing import Process
import setproctitle

from gi.repository import GLib

class PopenPlayerProcess:
    def __init__(self, name, target_module, use_x11=False):
        self.name = name
        self.target_module = target_module
        self.use_x11 = use_x11
        self.proc = None

    def start(self):
        env = os.environ.copy()
        if self.use_x11 and sys.platform != "win32" and env.get("DISPLAY"):
            env["GDK_BACKEND"] = "x11"
            env.pop("WAYLAND_DISPLAY", None)
        # Suppress VLC Log
        env["VLC_VERBOSE"] = "-1"
        module_dir = os.path.abspath(os.path.dirname(__file__))
        bootstrap = (
            "import multiprocessing as mp; "
            "import sys; "
            f"sys.path.insert(0, {module_dir!r}); "
            f"mp.current_process().name = {self.name!r}; "
            f"from {self.target_module} import main; "
            "main()"
        )
        cmd = [
            sys.executable,
            "-c",
            bootstrap,
        ]
        self.proc = subprocess.Popen(cmd, env=env)

    def terminate(self):
        if self.proc:
            self.proc.terminate()

    def kill(self):
        if self.proc:
            self.proc.kill()

    def join(self, timeout=None):
        if self.proc:
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass

    def is_alive(self):
        if self.proc:
            return self.proc.poll() is None
        return False

    @property
    def exitcode(self):
        if self.proc:
            return self.proc.poll()
        return None

try:
    from commons import *
    from ipc import get_service, publish_service
    # GStreamer backend check - check file existence instead of importing
    # to avoid GTK initialization in the server process.
    gst_player_path = os.path.join(os.path.dirname(__file__), "player", "gst_video_player.py")
    gst_video_player_main = object() if os.path.isfile(gst_player_path) else None
    gst_video_player_available = gst_video_player_main is not None

    from menu import show_systray_icon
    from monitor import *
    from utils import ConfigUtil, EndSessionHandler, get_video_paths, run_runtime_self_repair, normalize_video_file, video_needs_normalization, is_usable_video_path, build_reverse_video
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import *
    from wallblazer.ipc import get_service, publish_service
    # GStreamer backend check for installed mode
    gst_player_path = os.path.join(os.path.dirname(__file__), "player", "gst_video_player.py")
    gst_video_player_main = object() if os.path.isfile(gst_player_path) else None
    gst_video_player_available = gst_video_player_main is not None
    from wallblazer.menu import show_systray_icon
    from wallblazer.monitor import *
    from wallblazer.utils import ConfigUtil, EndSessionHandler, get_video_paths, run_runtime_self_repair, normalize_video_file, video_needs_normalization, is_usable_video_path, build_reverse_video

loop = GLib.MainLoop()
logger = logging.getLogger(LOGGER_NAME)
_REVERSE_FILTER = "reverse"
_REVERSE_X264_PRESET = "medium"
_REVERSE_X264_CRF = "16"


def _is_x11_session():
    session_type = str(os.environ.get("XDG_SESSION_TYPE", "")).strip().lower()
    if session_type == "x11":
        return True
    if session_type == "wayland":
        return False
    # Some display managers do not export XDG_SESSION_TYPE.  In that case an
    # X display without a Wayland socket is the best available signal.
    return bool(os.environ.get("DISPLAY")) and not bool(os.environ.get("WAYLAND_DISPLAY"))


def _vlc_video_player_available():
    try:
        return importlib.util.find_spec("vlc") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _prefer_gstreamer_video_backend():
    gst_available = bool(gst_video_player_available or (globals().get("gst_video_player_main") is not None))
    backend = str(os.environ.get("WALLBLAZER_VIDEO_BACKEND", "auto")).strip().lower()
    if backend == "vlc":
        return False
    if backend == "gst":
        return gst_available
    if not gst_available or sys.platform == "win32":
        return False
    # VLC's X11 renderer is substantially lighter than gtkglsink on NVIDIA
    # systems and has reliable wallpaper embedding there.  Keep GStreamer as
    # the automatic fallback on Wayland and when python-vlc is unavailable;
    # WALLBLAZER_VIDEO_BACKEND=gst remains an explicit escape hatch.
    if _is_x11_session() and _vlc_video_player_available():
        return False
    return True


def _has_non_default_video_adjustments(config):
    if not isinstance(config, dict):
        return False
    raw = config.get(CONFIG_KEY_VIDEO_ADJUSTMENTS, {})
    if not isinstance(raw, dict):
        return False
    for key, default in DEFAULT_VIDEO_ADJUSTMENTS.items():
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        if abs(value - float(default)) > 1e-3:
            return True
    return False


_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _is_truthy_env(var_name):
    value = str(os.environ.get(var_name, "")).strip().lower()
    return value in _TRUTHY_ENV_VALUES


def _is_auto_normalize_enabled():
    # Normalizing videos is expensive and can delay startup noticeably.
    # Keep it opt-in for stable low-resource runtime behavior.
    return _is_truthy_env("WALLBLAZER_AUTO_NORMALIZE")


def _safe_process_exitcode(proc):
    if proc is None:
        return None
    try:
        exit_code = getattr(proc, "exitcode", None)
    except Exception:
        exit_code = None
    if exit_code is not None:
        return exit_code
    raw_proc = getattr(proc, "proc", None)
    if raw_proc is not None:
        try:
            return raw_proc.poll()
        except Exception:
            return None
    return None


def _merge_pythonpath(existing_value, *extra_paths):
    merged = []
    for path in extra_paths:
        if not isinstance(path, str):
            continue
        value = path.strip()
        if not value or value in merged:
            continue
        merged.append(value)
    if isinstance(existing_value, str):
        for path in existing_value.split(os.pathsep):
            value = path.strip()
            if not value or value in merged:
                continue
            merged.append(value)
    return os.pathsep.join(merged)


def _build_gui_launch_command(pkgdatadir, localedir):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--gui-only", pkgdatadir, localedir]

    module_main = os.path.join(os.path.dirname(__file__), "__main__.py")
    if os.path.isfile(module_main):
        return [sys.executable, module_main, "--gui-only", pkgdatadir, localedir]

    if sys.platform == "win32":
        return [sys.executable, os.path.abspath(sys.argv[0]), "--gui-only", pkgdatadir, localedir]

    launcher = shutil.which("wallblazer")
    if launcher:
        return [launcher, "--gui-only", pkgdatadir, localedir]
    return [sys.executable, os.path.abspath(sys.argv[0]), "--gui-only", pkgdatadir, localedir]


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
        <method name="apply_video_profile"/>
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
        # Use a safe multiprocessing start method per platform.
        # The GUI is launched via subprocess.Popen so GTK gets a clean process.
        start_method = "spawn" if sys.platform == "win32" else "forkserver"
        try:
            if mp.get_start_method(allow_none=True) is None:
                mp.set_start_method(start_method)
        except RuntimeError:
            pass
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
        self._reverse_prebuild_lock = threading.Lock()
        self._reverse_prebuild_pending = []
        self._reverse_prebuild_thread = None

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

    def _playlist_interval_seconds(self):
        try:
            return max(0, int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300)))
        except (TypeError, ValueError):
            return 300

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
        if bool(self.config.get(CONFIG_KEY_PLAYLIST, False)):
            if bool(self.config.get(CONFIG_KEY_REVERSE_PLAYLIST, False)):
                return True
            playlist_name = self._get_monitor_playlist_name(monitor_name)
            if not playlist_name:
                return False
            return base_source in self._reverse_items_for_playlist(playlist_name)
        return bool(self.config.get(CONFIG_KEY_REVERSE_SINGLE, False))

    def _next_playlist_source(self, monitor_name, current_source):
        videos = self._get_monitor_playlist_videos(monitor_name)
        if not videos:
            return None
        shuffle = bool(self.config.get(CONFIG_KEY_PLAYLIST_SHUFFLE, False))
        if shuffle:
            if len(videos) == 1:
                return videos[0]
            candidates = [video for video in videos if video != current_source]
            return random.choice(candidates or videos)
        if current_source in videos:
            idx = videos.index(current_source)
            return videos[(idx + 1) % len(videos)]
        return videos[0]

    @staticmethod
    def _reverse_cache_key(video_path):
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
        cache_dir = os.path.join(CONFIG_DIR, "reverse-cache")
        return os.path.join(cache_dir, f"rev-{cache_key}.mp4")

    def _build_reverse_media_sync(self, video_path):
        if not is_usable_video_path(video_path):
            return None
        target = self._reverse_media_path(video_path)
        if not target:
            return None
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return target

        os.makedirs(os.path.dirname(target), exist_ok=True)
        target_root, target_ext = os.path.splitext(target)
        tmp_target = f"{target_root}.tmp-{os.getpid()}-{int(time.time() * 1000)}{target_ext or '.mp4'}"
        try:
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
                if os.path.exists(tmp_target):
                    os.remove(tmp_target)
                return None
            os.replace(tmp_target, target)
            return target
        finally:
            if os.path.exists(tmp_target):
                try:
                    os.remove(tmp_target)
                except OSError:
                    pass

    def _prepare_reverse_cache_for_paths(self, paths, include_next=False):
        if self.config.get(CONFIG_KEY_MODE) != MODE_VIDEO:
            return
        ordered_targets = []
        seen = set()

        for monitor_name, base_source in paths.items():
            if not isinstance(monitor_name, str) or not isinstance(base_source, str) or not base_source:
                continue
            if self._reverse_enabled_for_monitor(monitor_name, base_source) and base_source not in seen:
                ordered_targets.append(base_source)
                seen.add(base_source)
            if include_next:
                next_source = self._next_playlist_source(monitor_name, base_source)
                if (
                    isinstance(next_source, str)
                    and next_source
                    and self._reverse_enabled_for_monitor(monitor_name, next_source)
                    and next_source not in seen
                ):
                    ordered_targets.append(next_source)
                    seen.add(next_source)

        for video_path in ordered_targets:
            self._build_reverse_media_sync(video_path)

    def _all_reverse_targets_for_paths(self, paths):
        if self.config.get(CONFIG_KEY_MODE) != MODE_VIDEO:
            return []
        ordered_targets = []
        seen = set()

        for monitor_name, base_source in paths.items():
            if not isinstance(monitor_name, str):
                continue
            playlist_items = self._get_monitor_playlist_videos(monitor_name)
            if bool(self.config.get(CONFIG_KEY_PLAYLIST, False)) and playlist_items:
                for video_path in playlist_items:
                    if (
                        isinstance(video_path, str)
                        and video_path
                        and self._reverse_enabled_for_monitor(monitor_name, video_path)
                        and video_path not in seen
                    ):
                        ordered_targets.append(video_path)
                        seen.add(video_path)
                continue
            if (
                isinstance(base_source, str)
                and base_source
                and self._reverse_enabled_for_monitor(monitor_name, base_source)
                and base_source not in seen
            ):
                ordered_targets.append(base_source)
                seen.add(base_source)

        return ordered_targets

    def _start_reverse_prebuild(self, paths):
        targets = self._all_reverse_targets_for_paths(paths)
        if not targets:
            return

        with self._reverse_prebuild_lock:
            for video_path in targets:
                if video_path not in self._reverse_prebuild_pending:
                    self._reverse_prebuild_pending.append(video_path)
            if self._reverse_prebuild_thread and self._reverse_prebuild_thread.is_alive():
                return

            def _worker():
                while True:
                    with self._reverse_prebuild_lock:
                        if not self._reverse_prebuild_pending:
                            self._reverse_prebuild_thread = None
                            return
                        next_video = self._reverse_prebuild_pending.pop(0)
                    self._build_reverse_media_sync(next_video)

            self._reverse_prebuild_thread = threading.Thread(target=_worker, daemon=True)
            self._reverse_prebuild_thread.start()

    def _normalize_active_video_sources(self):
        if self.config.get(CONFIG_KEY_MODE) != MODE_VIDEO:
            return
        data_source = self.config.get(CONFIG_KEY_DATA_SOURCE, {})
        if not isinstance(data_source, dict):
            return

        normalized_cache = {}
        changed = False
        for monitor_name, source in list(data_source.items()):
            if not is_usable_video_path(source):
                continue
            if source in normalized_cache:
                new_source = normalized_cache[source]
            else:
                new_source = source
                if video_needs_normalization(source):
                    result = normalize_video_file(source, delete_original=True)
                    if result.get("ok"):
                        new_source = result.get("output_path") or source
                normalized_cache[source] = new_source
            if new_source != source:
                data_source[monitor_name] = new_source
                changed = True

        if changed:
            self.config[CONFIG_KEY_DATA_SOURCE] = data_source

    def _setup_player(self, mode, data_source=None, monitor=None):
        """Setup and run player"""
        logger.info(f"[Mode] {mode}")
        self.config[CONFIG_KEY_MODE] = mode

        # Set data source if specified
        if data_source is not None and monitor:
            self.config[CONFIG_KEY_DATA_SOURCE][monitor] = data_source
        if data_source is not None:
            self.config[CONFIG_KEY_DATA_SOURCE]['Default'] = data_source
        if mode == MODE_VIDEO and _is_auto_normalize_enabled():
            self._normalize_active_video_sources()
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

        prefer_gst_backend = mode == MODE_VIDEO and _prefer_gstreamer_video_backend()
        if (
            mode == MODE_VIDEO
            and not prefer_gst_backend
            and bool(gst_video_player_available)
            and _has_non_default_video_adjustments(self.config)
            and sys.platform != "win32"
        ):
            # Some Linux/X11 VLC output paths apply brightness/contrast/saturation
            # as display-level controls. Force the GStreamer backend when users
            # enable non-default video adjustments so effects stay wallpaper-local.
            logger.warning(
                "[Mode] Forcing GStreamer backend for video adjustments to avoid "
                "display-wide brightness/contrast side effects on VLC backend"
            )
            prefer_gst_backend = True

        if mode == MODE_VIDEO and prefer_gst_backend:
            logger.info("[Mode] Using GStreamer backend for local video playback")
            self.player_process = PopenPlayerProcess(
                name=f"wallblazer-player-{self._player_count}", target_module="player.gst_video_player", use_x11=False)
        elif mode in [MODE_VIDEO, MODE_STREAM]:
            self.player_process = PopenPlayerProcess(
                name=f"wallblazer-player-{self._player_count}", target_module="player.video_player", use_x11=True)
        elif mode == MODE_WEBPAGE:
            self.player_process = PopenPlayerProcess(
                name=f"wallblazer-player-{self._player_count}", target_module="player.web_player", use_x11=False)
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

        exit_code = _safe_process_exitcode(proc)
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

    def apply_video_profile(self):
        self._load_config()
        if (
            self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO
            and not _prefer_gstreamer_video_backend()
            and bool(gst_video_player_available)
            and _has_non_default_video_adjustments(self.config)
            and sys.platform != "win32"
        ):
            logger.info(
                "[Server] Reloading player with GStreamer backend so video adjustments stay wallpaper-local"
            )
            self.reload()
            return
        player = get_instance(DBUS_NAME_PLAYER)
        if player is None:
            if self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
                self.reload()
            return
        try:
            player.apply_video_profile()
        except Exception as e:
            logger.warning(f"[Server] apply_video_profile failed, reloading player: {e}")
            if self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
                self.reload()

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
            try:
                GLib.source_remove(self._playlist_timer_id)
            except Exception:
                pass
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
        """Advance each monitor when the timed playlist interval expires."""
        self._load_config()
        interval = int(self.config.get(CONFIG_KEY_PLAYLIST_INTERVAL, 300))
        player = get_instance(DBUS_NAME_PLAYER)
        if (
            interval <= 0
            and player is not None
            and self.config.get(CONFIG_KEY_MODE) == MODE_VIDEO
        ):
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
            current_src = paths.get(monitor_name, "")
            if shuffle:
                if len(existing) == 1:
                    next_video = existing[0]
                else:
                    candidates = [video for video in existing if video != current_src]
                    next_video = random.choice(candidates or existing)
            else:
                if current_src in existing:
                    idx = existing.index(current_src)
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
        changed = False
        last_video = None
        for monitor in monitors:
            file_list = get_video_paths()
            # Remove current data source from the random selection
            current_src = self.config.get(CONFIG_KEY_DATA_SOURCE, {}).get(monitor, "")
            if current_src in file_list:
                file_list.remove(current_src)
            if file_list:
                video_path = random.choice(file_list)
                self.config[CONFIG_KEY_MODE] = MODE_VIDEO
                self.config[CONFIG_KEY_DATA_SOURCE][monitor] = video_path
                last_video = video_path
                changed = True

        if changed:
            if last_video is not None:
                self.config[CONFIG_KEY_DATA_SOURCE]['Default'] = last_video
            self._save_config()
            self.video()

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

        cmd = _build_gui_launch_command(self.pkgdatadir, self.localedir)
        env = __import__("os").environ.copy()
        if sys.platform != "win32":
            env.pop("GDK_BACKEND", None)  # let GUI use native Wayland or X11
        env["VLC_VERBOSE"] = "-1"
        module_dir = os.path.dirname(__file__)
        module_parent = os.path.dirname(module_dir)
        env["PYTHONPATH"] = _merge_pythonpath(
            env.get("PYTHONPATH", ""),
            module_dir,
            module_parent,
            self.pkgdatadir,
        )
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
    return get_service(dbus_name)


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
        if sys.platform == "win32":
            subprocess.run(
                [
                    "taskkill",
                    "/F",
                    "/FI",
                    "IMAGENAME eq Wall-Blazer.exe",
                    "/FI",
                    f"PID ne {os.getpid()}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
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

    server = WallBlazerServer(version, pkgdatadir, localedir, args)
    try:
        service_handle = publish_service(DBUS_NAME_SERVER, server)
        loop.run()
    except Exception:
        raise Exception("[Server] Failed to publish IPC service – another instance may be running.")
