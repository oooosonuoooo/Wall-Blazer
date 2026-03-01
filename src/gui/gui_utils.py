import os
import sys
import hashlib
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import gi
from gi.repository import Gio, GdkPixbuf, GLib

try:
    import os as _os
    sys.path.insert(1, _os.path.join(sys.path[0], '..'))
    from commons import *
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import *

logger = logging.getLogger(LOGGER_NAME)

# Cache directory for thumbnails
_DEFAULT_THUMB_CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")),
    "wallblazer", "thumbs"
)


def _ensure_cache_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        fallback = os.path.join("/tmp", "wallblazer-cache", "thumbs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


THUMB_CACHE_DIR = _ensure_cache_dir(_DEFAULT_THUMB_CACHE_DIR)
THUMBNAIL_SEMAPHORE = threading.Semaphore(3)
THUMBNAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wb-thumb")


def _thumb_path_for(video_path: str) -> str:
    """Return the cached thumbnail PNG path for a given video file."""
    try:
        stat = os.stat(video_path)
        token = f"{video_path}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        token = video_path
    key = hashlib.md5(token.encode()).hexdigest()
    return os.path.join(THUMB_CACHE_DIR, key + ".png")


def _probe_duration_seconds(filename: str) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filename,
            ],
            stderr=subprocess.STDOUT,
            timeout=8,
            text=True,
        ).strip()
        value = float(out)
        return value if value > 0 else 0.0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return 0.0


def _thumbnail_commands(filename: str, thumb: str, duration_sec: float):
    common = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    filters = "thumbnail,scale=320:-1:flags=lanczos"

    cmds = []
    if duration_sec > 0.0:
        seek = max(0.0, min(duration_sec * 0.2, duration_sec - 0.05))
        cmds.append(common + ["-ss", f"{seek:.3f}", "-i", filename, "-frames:v", "1", "-an", "-vf", filters, thumb])
    cmds.append(common + ["-ss", "1", "-i", filename, "-frames:v", "1", "-an", "-vf", filters, thumb])
    cmds.append(common + ["-i", filename, "-frames:v", "1", "-an", "-vf", filters, thumb])
    cmds.append(common + ["-ss", "0", "-i", filename, "-frames:v", "1", "-an", "-vf", "scale=320:-1:flags=lanczos", thumb])
    return cmds


def generate_thumbnail(filename: str) -> str | None:
    """
    Generate a preview thumbnail for a video file using ffmpeg.
    Returns the path to the generated PNG, or None on failure.
    The thumbnail is cached on disk so it's only generated once.
    """
    thumb = _thumb_path_for(filename)
    if os.path.exists(thumb):
        return thumb

    duration_sec = _probe_duration_seconds(filename)
    try:
        for cmd in _thumbnail_commands(filename, thumb, duration_sec):
            ret = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=25,
                check=False,
            )
            if ret.returncode == 0 and os.path.exists(thumb):
                return thumb
            if os.path.exists(thumb):
                try:
                    os.remove(thumb)
                except OSError:
                    pass
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"[Thumbnail] ffmpeg failed for {filename}: {e}")
    return None


def get_thumbnail(video_path: str, list_store, idx: int):
    """
    Load or generate a video thumbnail and update the list_store row.
    Designed to be called in a background thread.
    """
    try:
        with THUMBNAIL_SEMAPHORE:
            thumb = generate_thumbnail(video_path)
            if thumb is not None:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(thumb, 160, 90)

                def _update_list_store():
                    try:
                        if 0 <= idx < len(list_store):
                            list_store[idx][0] = pixbuf
                    except Exception as e:
                        logger.debug(f"[Thumbnail] UI update failed for {video_path}: {e}")
                    return False

                # Gtk list models must be updated on the main thread.
                GLib.idle_add(_update_list_store)
    except Exception as e:
        logger.debug(f"[Thumbnail] Could not load thumbnail for {video_path}: {e}")


def get_thumbnail_pixbuf(video_path: str, width: int = 240, height: int = 135):
    """Load (or generate) a cached static thumbnail pixbuf for a video file."""
    try:
        with THUMBNAIL_SEMAPHORE:
            thumb = generate_thumbnail(video_path)
            if thumb is None:
                return None
            return GdkPixbuf.Pixbuf.new_from_file_at_size(thumb, width, height)
    except Exception as e:
        logger.debug(f"[Thumbnail] Failed to load pixbuf for {video_path}: {e}")
    return None


def request_thumbnail_pixbuf(video_path: str, width: int, height: int, on_ready):
    """
    Resolve a thumbnail pixbuf in a bounded worker pool and dispatch callback on GTK thread.
    The callback signature must be: callback(pixbuf) -> bool|None
    """
    def _job():
        pixbuf = get_thumbnail_pixbuf(video_path, width=width, height=height)

        def _dispatch():
            try:
                on_ready(pixbuf)
            except Exception as e:
                logger.debug(f"[Thumbnail] on_ready callback failed for {video_path}: {e}")
            return False

        GLib.idle_add(_dispatch)

    THUMBNAIL_EXECUTOR.submit(_job)


def debounce(wait_time):
    """
    Decorator that debounces a function so it is only called after
    wait_time seconds of inactivity.
    """
    def decorator(function):
        def debounced(*args, **kwargs):
            def call_function():
                debounced._timer = None
                return function(*args, **kwargs)

            if debounced._timer is not None:
                debounced._timer.cancel()

            debounced._timer = threading.Timer(wait_time, call_function)
            debounced._timer.start()

        debounced._timer = None
        return debounced

    return decorator
