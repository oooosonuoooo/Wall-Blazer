import os
import sys
import hashlib
import logging
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib

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


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


_CPU_COUNT = os.cpu_count() or 4
THUMBNAIL_WORKERS = _env_int(
    "WALLBLAZER_THUMBNAIL_WORKERS",
    default=min(8, max(4, _CPU_COUNT + 2)),
    min_value=1,
    max_value=16,
)
THUMBNAIL_TIMEOUT_SEC = _env_int(
    "WALLBLAZER_THUMBNAIL_TIMEOUT_SEC",
    default=8,
    min_value=2,
    max_value=60,
)
THUMBNAIL_SIZE = _env_int(
    "WALLBLAZER_THUMBNAIL_SIZE",
    default=320,
    min_value=96,
    max_value=1024,
)

# Keep generation bounded, but use enough workers that large video folders warm quickly.
THUMBNAIL_SEMAPHORE = threading.Semaphore(THUMBNAIL_WORKERS)
THUMBNAIL_EXECUTOR = ThreadPoolExecutor(
    max_workers=THUMBNAIL_WORKERS,
    thread_name_prefix="wb-thumb",
)


def _thumb_path_for(video_path: str) -> str:
    """Return the cached thumbnail PNG path for a given video file."""
    try:
        stat = os.stat(video_path)
        token = f"{video_path}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        token = video_path
    key = hashlib.md5(token.encode()).hexdigest()
    return os.path.join(THUMB_CACHE_DIR, key + ".png")


def _is_valid_thumb(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _tmp_thumb_path(thumb: str) -> str:
    return f"{thumb}.tmp-{os.getpid()}-{threading.get_ident()}.png"


def _run_thumbnail_command(cmd: list[str], thumb: str) -> bool:
    tmp_thumb = _tmp_thumb_path(thumb)
    run_cmd = [tmp_thumb if arg == "{output}" else arg for arg in cmd]
    try:
        ret = subprocess.run(
            run_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=THUMBNAIL_TIMEOUT_SEC,
            check=False,
        )
        if ret.returncode == 0 and _is_valid_thumb(tmp_thumb):
            os.replace(tmp_thumb, thumb)
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f"[Thumbnail] command failed for {cmd[0]}: {e}")
    finally:
        if os.path.exists(tmp_thumb):
            try:
                os.remove(tmp_thumb)
            except OSError:
                pass
    return False


def _generate_with_ffmpegthumbnailer(filename: str, thumb: str) -> bool:
    if shutil.which("ffmpegthumbnailer") is None:
        return False
    cmd = [
        "ffmpegthumbnailer",
        "-i", filename,
        "-o", "{output}",
        "-s", str(THUMBNAIL_SIZE),
        "-c", "png",
        "-t", "10",
        "-q", "8",
    ]
    return _run_thumbnail_command(cmd, thumb)


def _ffmpeg_thumbnail_commands(filename: str):
    common = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    filters = f"scale={THUMBNAIL_SIZE}:-1:flags=fast_bilinear"
    return [
        common + ["-ss", "1", "-i", filename, "-frames:v", "1", "-an", "-vf", filters, "{output}"],
        common + ["-i", filename, "-frames:v", "1", "-an", "-vf", filters, "{output}"],
        common + ["-ss", "0", "-i", filename, "-frames:v", "1", "-an", "-vf", filters, "{output}"],
    ]


def _generate_with_ffmpeg(filename: str, thumb: str) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    for cmd in _ffmpeg_thumbnail_commands(filename):
        if _run_thumbnail_command(cmd, thumb):
            return True
    return False


def generate_thumbnail(filename: str) -> str | None:
    """
    Generate a preview thumbnail for a video file using ffmpeg.
    Returns the path to the generated PNG, or None on failure.
    The thumbnail is cached on disk so it's only generated once.
    """
    thumb = _thumb_path_for(filename)
    if _is_valid_thumb(thumb):
        return thumb

    try:
        if _generate_with_ffmpegthumbnailer(filename, thumb):
            return thumb
        if _generate_with_ffmpeg(filename, thumb):
            return thumb
    except OSError as e:
        logger.debug(f"[Thumbnail] failed for {filename}: {e}")
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
        thumb = _thumb_path_for(video_path)
        if not _is_valid_thumb(thumb):
            with THUMBNAIL_SEMAPHORE:
                thumb = generate_thumbnail(video_path)
        if thumb is None or not _is_valid_thumb(thumb):
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

    Thread-safe: uses a Lock to guard the internal timer reference so that
    concurrent calls from different threads cannot race on cancel/create.
    """
    def decorator(function):
        _lock = threading.Lock()

        def debounced(*args, **kwargs):
            def call_function():
                with _lock:
                    debounced._timer = None
                return function(*args, **kwargs)

            with _lock:
                if debounced._timer is not None:
                    debounced._timer.cancel()
                debounced._timer = threading.Timer(wait_time, call_function)
                debounced._timer.start()

        debounced._timer = None
        return debounced

    return decorator
