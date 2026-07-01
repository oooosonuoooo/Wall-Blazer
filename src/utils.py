import json
import logging
import shlex
import shutil
import subprocess
import pathlib
import glob
import threading
import tempfile
import math

import sys
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck
except ValueError:
    pass
from gi.repository import Gio, GLib, Gtk

try:
    import pydbus
except ImportError:
    pydbus = None

try:
    from commons import *
except (ModuleNotFoundError, ImportError):
    from wallblazer.commons import *

logger = logging.getLogger(LOGGER_NAME)

VIDEO_FILE_EXTENSIONS = {
    ".3g2", ".3gp", ".asf", ".avi", ".f4v", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".mxf", ".ogg", ".ogm", ".ogv",
    ".qt", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}
REQUIRED_RUNTIME_BINARIES = ["vlc", "ffmpeg", "ffprobe", "yt-dlp"]
TRANSIENT_VIDEO_MARKERS = (
    ".tmp-",
    ".part",
    ".partial",
    ".crdownload",
    ".download",
)


def _resolve_thumb_cache_dir():
    preferred = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")),
        "wallblazer",
        "thumbs",
    )
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except OSError:
        fallback = os.path.join("/tmp", "wallblazer-cache", "thumbs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


THUMB_CACHE_DIR = _resolve_thumb_cache_dir()
_GPU_PROFILE_CACHE = None
_MEDIA_INFO_CACHE = {}
_MEDIA_INFO_CACHE_LOCK = threading.Lock()
GPU_VENDOR_MAP = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x1022": "amd",
    "0x8086": "intel",
}


def _truthy_env(var_name):
    return str(os.environ.get(var_name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _rewrite_broken_desktop_exec(desktop_path, default_exec):
    if not isinstance(desktop_path, str) or not os.path.isfile(desktop_path):
        return False
    try:
        with open(desktop_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False

    changed = False
    new_lines = []
    for line in lines:
        if not line.startswith("Exec="):
            new_lines.append(line)
            continue
        raw_exec = line[len("Exec="):].strip()
        try:
            parts = shlex.split(raw_exec)
        except ValueError:
            parts = raw_exec.split()
        if not parts:
            new_lines.append(line)
            continue
        command = parts[0]
        if (
            os.path.isabs(command)
            and not os.path.exists(command)
            and "wallblazer" in os.path.basename(command).lower()
        ):
            new_lines.append(f"Exec={default_exec}\n")
            changed = True
            continue
        new_lines.append(line)

    if not changed:
        return False
    try:
        with open(desktop_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        logger.info(f"[Repair] Repaired launcher entry: {desktop_path}")
        return True
    except OSError as e:
        logger.warning(f"[Repair] Could not update launcher entry '{desktop_path}': {e}")
        return False


def _repair_user_wallblazer_launchers():
    changed = 0
    changed += int(_rewrite_broken_desktop_exec(LOCAL_APPLICATION_DESKTOP_PATH, "wallblazer"))
    changed += int(_rewrite_broken_desktop_exec(AUTOSTART_DESKTOP_PATH, "wallblazer -b"))
    return changed


def _cleanup_reverse_cache_dir():
    cache_dir = os.path.join(CONFIG_DIR, "reverse-cache")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return {"removed_files": 0, "removed_bytes": 0}

    removed_files = 0
    removed_bytes = 0
    entries = []
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if ".tmp-" in name or stat.st_size <= 0:
            try:
                removed_bytes += stat.st_size
                os.remove(path)
                removed_files += 1
            except OSError:
                pass
            continue
        entries.append((path, stat.st_mtime, stat.st_size))

    max_cache_mb = 768
    try:
        max_cache_mb = max(128, int(float(os.environ.get("WALLBLAZER_REVERSE_CACHE_MB", max_cache_mb))))
    except (TypeError, ValueError):
        pass
    max_cache_bytes = max_cache_mb * 1024 * 1024
    total_bytes = sum(size for _, _, size in entries)
    if total_bytes > max_cache_bytes:
        for path, _mtime, size in sorted(entries, key=lambda item: item[1]):
            if total_bytes <= max_cache_bytes:
                break
            try:
                os.remove(path)
                total_bytes -= size
                removed_bytes += size
                removed_files += 1
            except OSError:
                pass

    return {"removed_files": removed_files, "removed_bytes": removed_bytes}


def _run_text_cmd(args, timeout=5):
    try:
        ret = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        if ret.returncode == 0:
            return ret.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _safe_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _safe_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _cache_key_for_path(path):
    try:
        st = os.stat(path)
        return (os.path.realpath(path), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (os.path.realpath(path), None, None)


def _probe_media_info_uncached(path):
    info = {
        "width": None,
        "height": None,
        "duration": None,
        "codec": None,
        "pix_fmt": None,
        "avg_frame_rate": None,
    }
    if not isinstance(path, str) or not path:
        return info

    try:
        raw = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,codec_name,pix_fmt,avg_frame_rate",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            stderr=subprocess.DEVNULL,
            timeout=8,
            text=True,
        )
        data = json.loads(raw)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return info

    streams = data.get("streams", [])
    if isinstance(streams, list) and streams:
        stream = streams[0] if isinstance(streams[0], dict) else {}
        info["width"] = _safe_int(stream.get("width"))
        info["height"] = _safe_int(stream.get("height"))
        codec = stream.get("codec_name")
        if isinstance(codec, str) and codec.strip():
            info["codec"] = codec.strip().lower()
        pix_fmt = stream.get("pix_fmt")
        if isinstance(pix_fmt, str) and pix_fmt.strip():
            info["pix_fmt"] = pix_fmt.strip().lower()
        fps_raw = stream.get("avg_frame_rate")
        if isinstance(fps_raw, str) and fps_raw and fps_raw != "0/0":
            if "/" in fps_raw:
                num, den = fps_raw.split("/", 1)
                num_f = _safe_float(num)
                den_f = _safe_float(den)
                if num_f is not None and den_f not in (None, 0.0):
                    info["avg_frame_rate"] = num_f / den_f
            else:
                info["avg_frame_rate"] = _safe_float(fps_raw)

    fmt = data.get("format", {})
    if isinstance(fmt, dict):
        info["duration"] = _safe_float(fmt.get("duration"))

    return info


def is_transient_video_path(path):
    """
    Return True for temporary/incomplete media files that should never be used
    as wallpaper sources (e.g. ffmpeg temp outputs).
    """
    if not isinstance(path, str) or not path:
        return False
    name = pathlib.Path(path).name.lower()
    if not name:
        return False
    return any(marker in name for marker in TRANSIENT_VIDEO_MARKERS)


def is_usable_video_path(path):
    """
    Return True only for existing, non-transient local files.
    """
    return isinstance(path, str) and path and os.path.isfile(path) and not is_transient_video_path(path)


def probe_media_info(path, force_refresh=False):
    """
    Return cached ffprobe metadata for a local media file.
    """
    if not isinstance(path, str) or not path:
        return _probe_media_info_uncached(path)

    cache_key = _cache_key_for_path(path)
    if not force_refresh:
        with _MEDIA_INFO_CACHE_LOCK:
            cached = _MEDIA_INFO_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    info = _probe_media_info_uncached(path)
    with _MEDIA_INFO_CACHE_LOCK:
        _MEDIA_INFO_CACHE[cache_key] = dict(info)
    return dict(info)


def _ffmpeg_run_quiet(args, timeout=None, low_priority=True):
    run_kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "check": False,
    }
    if timeout is not None:
        run_kwargs["timeout"] = timeout
    if low_priority and sys.platform != "win32":
        def _lower_priority():
            try:
                os.nice(10)
            except OSError:
                pass
        run_kwargs["preexec_fn"] = _lower_priority
    return subprocess.run(args, **run_kwargs)


def _ffconcat_quote(path):
    return str(path).replace("'", "'\\''")


def _build_reverse_video_single_pass(
    input_path,
    output_path,
    *,
    reverse_filter,
    preset,
    crf,
    pix_fmt,
    threads,
    timeout_sec,
    low_priority,
):
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-vf",
        reverse_filter,
        "-fps_mode",
        "passthrough",
        "-an",
        "-threads",
        str(threads),
        "-c:v",
        "libx264",
        "-preset",
        str(preset),
        "-crf",
        str(crf),
        "-pix_fmt",
        str(pix_fmt),
        "-movflags",
        "+faststart",
        output_path,
    ]
    ret = _ffmpeg_run_quiet(
        cmd,
        timeout=timeout_sec,
        low_priority=low_priority,
    )
    return ret.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0


def build_reverse_video(
    input_path,
    output_path,
    *,
    reverse_filter="reverse",
    preset="medium",
    crf="16",
    pix_fmt="yuv420p",
    threads=1,
    timeout_sec=60 * 60,
    low_priority=True,
):
    """
    Build a reversed MP4 cache with quality-first settings and resilient chunking.
    Returns a result dict with keys: ok (bool), error (str|None), chunk_sec (float|None).
    """
    result = {"ok": False, "error": None, "chunk_sec": None}
    if not is_usable_video_path(input_path):
        result["error"] = "missing source file"
        return result
    if shutil.which("ffmpeg") is None:
        result["error"] = "ffmpeg not found"
        return result
    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    except OSError as e:
        result["error"] = str(e)
        return result

    info = probe_media_info(input_path)
    duration = _safe_float(info.get("duration"))
    width = _safe_int(info.get("width"))
    height = _safe_int(info.get("height"))
    fps = _safe_float(info.get("avg_frame_rate"))
    if duration is None or duration <= 0:
        result["error"] = "reverse transcode failed"
        return result
    if fps is None or fps <= 0:
        fps = 30.0
    fps = max(1.0, min(120.0, float(fps)))
    if width is None or width <= 0:
        width = 1280
    if height is None or height <= 0:
        height = 720

    total_reverse_buffer_mb = (
        float(width) * float(height) * 1.5 * float(fps) * float(duration)
    ) / (1024.0 * 1024.0)
    onepass_max_mb = 256.0
    onepass_max_sec = 12.0
    env_onepass_mb = _safe_float(os.environ.get("WALLBLAZER_REVERSE_ONEPASS_MB"))
    env_onepass_sec = _safe_float(os.environ.get("WALLBLAZER_REVERSE_ONEPASS_MAX_SEC"))
    if env_onepass_mb is not None and env_onepass_mb > 0:
        onepass_max_mb = env_onepass_mb
    if env_onepass_sec is not None and env_onepass_sec > 0:
        onepass_max_sec = env_onepass_sec
    onepass_max_mb = max(64.0, min(4096.0, onepass_max_mb))
    onepass_max_sec = max(1.0, min(300.0, onepass_max_sec))

    should_try_onepass = duration <= onepass_max_sec and total_reverse_buffer_mb <= onepass_max_mb
    if should_try_onepass and _build_reverse_video_single_pass(
        input_path,
        output_path,
        reverse_filter=reverse_filter,
        preset=preset,
        crf=crf,
        pix_fmt=pix_fmt,
        threads=threads,
        timeout_sec=timeout_sec,
        low_priority=low_priority,
    ):
        result["ok"] = True
        return result

    default_chunk_sec = 6.0
    env_chunk = _safe_float(os.environ.get("WALLBLAZER_REVERSE_CHUNK_SEC"))
    if env_chunk is not None and env_chunk > 0:
        default_chunk_sec = env_chunk
    default_chunk_sec = max(0.25, min(30.0, default_chunk_sec))

    chunk_mem_budget_mb = 256.0
    env_mem_budget = _safe_float(os.environ.get("WALLBLAZER_REVERSE_CHUNK_MEM_MB"))
    if env_mem_budget is not None and env_mem_budget > 0:
        chunk_mem_budget_mb = env_mem_budget
    chunk_mem_budget_mb = max(64.0, min(4096.0, chunk_mem_budget_mb))

    mb_per_sec = (
        float(width) * float(height) * 1.5 * float(fps)
    ) / (1024.0 * 1024.0)
    dynamic_chunk_sec = default_chunk_sec
    if mb_per_sec > 0:
        dynamic_chunk_sec = min(default_chunk_sec, chunk_mem_budget_mb / mb_per_sec)
    dynamic_chunk_sec = max(0.1, min(default_chunk_sec, dynamic_chunk_sec))

    # Retry chunking with smaller pieces if memory pressure still causes failures.
    max_attempts = 4
    chunk_sec = dynamic_chunk_sec
    for _attempt in range(max_attempts):
        tmp_dir = tempfile.mkdtemp(prefix="wallblazer-rev-")
        try:
            segment_paths = []
            num_segments = max(1, int(math.ceil(duration / chunk_sec)))
            for idx in range(num_segments):
                seg_start = float(idx) * float(chunk_sec)
                seg_duration = min(float(chunk_sec), float(duration) - seg_start)
                if seg_duration <= 0.02:
                    continue
                seg_path = os.path.join(tmp_dir, f"seg-{idx:05d}.mp4")
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{seg_start:.6f}",
                    "-i",
                    input_path,
                    "-t",
                    f"{seg_duration:.6f}",
                    "-map",
                    "0:v:0",
                    "-vf",
                    reverse_filter,
                    "-fps_mode",
                    "passthrough",
                    "-an",
                    "-threads",
                    str(threads),
                    "-c:v",
                    "libx264",
                    "-preset",
                    str(preset),
                    "-crf",
                    str(crf),
                    "-pix_fmt",
                    str(pix_fmt),
                    "-movflags",
                    "+faststart",
                    seg_path,
                ]
                ret = _ffmpeg_run_quiet(
                    cmd,
                    timeout=timeout_sec,
                    low_priority=low_priority,
                )
                if ret.returncode != 0 or not os.path.isfile(seg_path) or os.path.getsize(seg_path) <= 0:
                    segment_paths = []
                    break
                segment_paths.append(seg_path)

            if not segment_paths:
                chunk_sec = max(0.05, chunk_sec / 2.0)
                continue

            concat_path = os.path.join(tmp_dir, "concat.txt")
            with open(concat_path, "w", encoding="utf-8") as f:
                for seg in reversed(segment_paths):
                    f.write(f"file '{_ffconcat_quote(seg)}'\n")

            concat_cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                output_path,
            ]
            concat_ret = _ffmpeg_run_quiet(
                concat_cmd,
                timeout=timeout_sec,
                low_priority=low_priority,
            )
            if concat_ret.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                # Fallback: concat by re-encoding once in case timestamp copy fails.
                concat_reencode_cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    concat_path,
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    str(preset),
                    "-crf",
                    str(crf),
                    "-pix_fmt",
                    str(pix_fmt),
                    "-movflags",
                    "+faststart",
                    output_path,
                ]
                concat_ret = _ffmpeg_run_quiet(
                    concat_reencode_cmd,
                    timeout=timeout_sec,
                    low_priority=low_priority,
                )
            if concat_ret.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                result["ok"] = True
                result["chunk_sec"] = round(chunk_sec, 4)
                return result
            chunk_sec = max(0.05, chunk_sec / 2.0)
        except (OSError, subprocess.TimeoutExpired) as e:
            result["error"] = str(e)
            chunk_sec = max(0.05, chunk_sec / 2.0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if result["error"] is None:
        result["error"] = "reverse transcode failed"
    return result


def open_path_default(path):
    """
    Open a local file or directory with the platform default handler.
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")

    if sys.platform == "win32":
        os.startfile(path)
        return None

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    return subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def video_needs_normalization(path):
    """
    Return True when a file is not already an mp4 encoded at exactly 1920x1080.
    """
    if not is_usable_video_path(path):
        return False
    info = probe_media_info(path)
    ext = pathlib.Path(path).suffix.lower()
    return (
        ext != ".mp4"
        or info.get("width") != 1920
        or info.get("height") != 1080
    )


def _normalized_mp4_path(path):
    source = pathlib.Path(path)
    if source.suffix.lower() == ".mp4":
        return str(source)
    candidate = source.with_suffix(".mp4")
    if not candidate.exists():
        return str(candidate)

    index = 2
    while True:
        alt = candidate.with_name(f"{candidate.stem} {index}.mp4")
        if not alt.exists():
            return str(alt)
        index += 1


def normalize_video_file(path, delete_original=True):
    """
    Transcode a file into a 1920x1080 mp4 while preserving the source FPS.

    Returns:
      {
        "ok": bool,
        "changed": bool,
        "input_path": str,
        "output_path": str,
        "deleted_original": bool,
        "error": str | None,
      }
    """
    result = {
        "ok": False,
        "changed": False,
        "input_path": path,
        "output_path": path,
        "deleted_original": False,
        "error": None,
    }
    if not is_usable_video_path(path):
        result["error"] = "missing source file"
        return result

    if not video_needs_normalization(path):
        result["ok"] = True
        return result

    output_path = _normalized_mp4_path(path)
    if os.path.realpath(output_path) == os.path.realpath(path) and not delete_original:
        source = pathlib.Path(path)
        output_path = str(source.with_name(f"{source.stem} 1080p.mp4"))
    result["output_path"] = output_path

    out_root, out_ext = os.path.splitext(output_path)
    tmp_target = f"{out_root}.tmp-{os.getpid()}-{threading.get_ident()}{out_ext or '.mp4'}"
    vf = (
        "scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1920:1080"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-vf",
        vf,
        "-fps_mode",
        "passthrough",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        tmp_target,
    ]

    try:
        ret = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60 * 60,
            check=False,
        )
        if ret.returncode != 0 or not os.path.isfile(tmp_target) or os.path.getsize(tmp_target) <= 0:
            result["error"] = "ffmpeg normalization failed"
            return result

        if os.path.realpath(output_path) == os.path.realpath(path):
            backup = f"{path}.orig-{os.getpid()}-{threading.get_ident()}"
            os.replace(path, backup)
            os.replace(tmp_target, output_path)
            if delete_original:
                os.remove(backup)
                result["deleted_original"] = True
            else:
                os.replace(backup, path)
        else:
            os.replace(tmp_target, output_path)
            if delete_original:
                os.remove(path)
                result["deleted_original"] = True

        result["ok"] = True
        result["changed"] = True
        return result
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        result["error"] = str(e)
        return result
    finally:
        if os.path.exists(tmp_target):
            try:
                os.remove(tmp_target)
            except OSError:
                pass


def _redacted_config_summary(config):
    """
    Return a privacy-safe config summary without local paths, URLs, or monitor names.
    """
    if not isinstance(config, dict):
        return {"valid": False}

    data_source = config.get(CONFIG_KEY_DATA_SOURCE, {})
    if not isinstance(data_source, dict):
        data_source = {}
    monitor_count = len(
        [name for name in data_source.keys() if isinstance(name, str) and name != "Default"]
    )

    playlist_library = config.get(CONFIG_KEY_PLAYLIST_LIBRARY, {})
    if not isinstance(playlist_library, dict):
        playlist_library = {}
    playlist_count = len(playlist_library)
    playlist_item_count = 0
    for items in playlist_library.values():
        if isinstance(items, list):
            playlist_item_count += len(items)

    return {
        "version": config.get(CONFIG_KEY_VERSION),
        "mode": config.get(CONFIG_KEY_MODE),
        "monitor_count": monitor_count,
        "has_default_source": bool(str(data_source.get("Default", "")).strip()),
        "playlist_count": playlist_count,
        "playlist_item_count": playlist_item_count,
        "playlist_enabled": bool(config.get(CONFIG_KEY_PLAYLIST, False)),
        "theme": config.get(CONFIG_KEY_THEME, "system"),
    }


def is_gnome():
    """
    Check if current DE is GNOME or not.
    On Ubuntu 20.04, $XDG_CURRENT_DESKTOP = ubuntu:GNOME
    On Fedora 34, $XDG_CURRENT_DESKTOP = GNOME
    Hence we do the detection by looking for the word "gnome"
    """
    return "gnome" in str(os.environ.get("XDG_CURRENT_DESKTOP") or '').lower()


def is_wayland():
    """
    Check if current session is Wayland or not.
    $XDG_SESSION_TYPE = x11 | wayland
    """
    return os.environ.get("XDG_SESSION_TYPE") == "wayland"


def is_nvidia_proprietary():
    """
    Check if the GPU is nvidia and the driver is proprietary
    """
    if sys.platform == "win32":
        return False
    if shutil.which("glxinfo") is None:
        logger.debug("[Utils] glxinfo not found, skipping GPU vendor check")
        return False
    ret = subprocess.run(["glxinfo", "-B"],
                         stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         text=True)
    if ret.returncode != 0:
        logger.debug(f"[Utils] glxinfo check failed with code={ret.returncode}")
        return False
    output = ret.stdout
    return "OpenGL vendor string: NVIDIA Corporation" in output


def is_vdpau_ok():
    """
    Check if the VDPAU works fine.

    vdpauinfo is optional, so missing binary should not be logged as an error.
    """
    if sys.platform == "win32":
        return False
    if shutil.which("vdpauinfo") is None:
        logger.debug("[Utils] vdpauinfo not found, skipping VDPAU check")
        return False
    ret = subprocess.run(["vdpauinfo"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.STDOUT)
    return ret.returncode == 0


def is_flatpak():
    """
    Check if Wall Blazer is a Flatpak
    Reference:
    https://gitlab.gnome.org/jrb/crosswords/-/blob/master/src/crosswords-init.c#L179
    """
    if sys.platform == "win32":
        return False
    return os.path.isfile('/.flatpak-info')


def detect_gpu_vendors():
    """
    Detect current GPU vendors from lspci/glxinfo best-effort.
    Returns a sorted list, e.g. ['amd', 'intel'].
    """
    vendors = set()

    lspci_output = _run_text_cmd(["lspci", "-nnk"], timeout=4)
    if lspci_output:
        for line in lspci_output.splitlines():
            low = line.lower()
            if (
                "vga compatible controller" not in low
                and "3d controller" not in low
                and "display controller" not in low
            ):
                continue
            for vendor in ("nvidia", "intel", "amd", "ati", "radeon", "vmware", "virtio"):
                if vendor in low:
                    vendors.add("amd" if vendor in {"ati", "radeon"} else vendor)

    glxinfo_output = _run_text_cmd(["glxinfo", "-B"], timeout=4)
    if glxinfo_output:
        low = glxinfo_output.lower()
        if "nvidia corporation" in low:
            vendors.add("nvidia")
        if "intel" in low:
            vendors.add("intel")
        if "amd" in low or "advanced micro devices" in low:
            vendors.add("amd")
        if "llvmpipe" in low or "software rasterizer" in low:
            vendors.add("software")

    return sorted(vendors)


def detect_hw_accel_methods():
    """
    Detect ffmpeg HW acceleration methods available on this system.
    Returns a sorted list, e.g. ['vaapi', 'vdpau'].
    """
    methods = set()
    ffmpeg_output = _run_text_cmd(["ffmpeg", "-hide_banner", "-hwaccels"], timeout=5)
    if ffmpeg_output:
        for line in ffmpeg_output.splitlines():
            item = line.strip().lower()
            if not item or item.startswith("hardware acceleration methods"):
                continue
            if " " in item:
                continue
            methods.add(item)

    if shutil.which("vainfo") is not None:
        ret = subprocess.run(
            ["vainfo"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ret.returncode == 0:
            methods.add("vaapi")

    if "vdpau" in methods and not is_vdpau_ok():
        methods.discard("vdpau")
    elif is_vdpau_ok():
        methods.add("vdpau")

    return sorted(methods)


def get_vlc_hwdec_profile(force_refresh=False):
    """
    Build a VLC hw-decode profile:
    - Prefer GPU decode when available.
    - Fall back to CPU decode (`none`) when no stable hw backend is detected.
    Environment override: WALLBLAZER_FORCE_HWDEC (e.g. any|none|vaapi|vdpau|drm).
    """
    global _GPU_PROFILE_CACHE
    if _GPU_PROFILE_CACHE is not None and not force_refresh:
        return dict(_GPU_PROFILE_CACHE)

    forced = str(os.environ.get("WALLBLAZER_FORCE_HWDEC", "")).strip().lower()
    if forced:
        profile = {
            "hwdec": forced,
            "gpu_available": forced != "none",
            "reason": f"forced via WALLBLAZER_FORCE_HWDEC={forced}",
            "vendors": detect_gpu_vendors(),
            "methods": detect_hw_accel_methods(),
        }
        _GPU_PROFILE_CACHE = profile
        return dict(profile)

    if sys.platform == "win32":
        default_hwdec = str(os.environ.get("WALLBLAZER_WIN_HWDEC", "none")).strip().lower() or "none"
        profile = {
            "hwdec": default_hwdec,
            "gpu_available": default_hwdec != "none",
            "reason": f"Windows stability default ({default_hwdec}). Set WALLBLAZER_FORCE_HWDEC to override.",
            "vendors": ["windows_gpu"],
            "methods": ["d3d11va", "dxva2"],
        }
        _GPU_PROFILE_CACHE = profile
        return dict(profile)

    methods = detect_hw_accel_methods()
    vendors = detect_gpu_vendors()

    hwdec = "none"
    reason = "No HW decoder detected, CPU fallback"
    has_nvidia = "nvidia" in vendors
    has_intel_amd = ("intel" in vendors) or ("amd" in vendors)

    allow_hybrid_hwdec = _truthy_env("WALLBLAZER_ALLOW_HYBRID_HWDEC")

    if has_nvidia and has_intel_amd and not allow_hybrid_hwdec:
        hwdec = "none"
        reason = (
            "Hybrid NVIDIA setup detected; defaulting to stable CPU decode. "
            "Set WALLBLAZER_ALLOW_HYBRID_HWDEC=1 to opt into GPU decode."
        )
    elif has_nvidia:
        # Prefer CUDA/NVDEC first on NVIDIA setups. "any" can pick VDPAU on
        # hybrid systems, which is less stable and can produce black output.
        if "cuda" in methods:
            hwdec = "cuda"
            reason = "Detected NVIDIA CUDA/NVDEC hardware decode path"
        elif has_intel_amd:
            for method in ("vaapi", "drm"):
                if method in methods:
                    hwdec = method
                    reason = f"Detected hybrid GPU; preferring {method} decode path"
                    break
            else:
                hwdec = "any" if methods else "none"
                reason = (
                    "Hybrid GPU detected; VLC auto-select HW decode backend"
                    if methods
                    else "No HW decoder detected, CPU fallback"
                )
        elif "vdpau" in methods and is_vdpau_ok():
            hwdec = "vdpau"
            reason = "Detected NVIDIA VDPAU hardware decode path"
        elif methods:
            hwdec = "any"
            reason = "Detected NVIDIA GPU; VLC auto-select HW decode backend"
    else:
        for method in ("vaapi", "drm", "vdpau"):
            if method in methods:
                hwdec = method
                reason = f"Detected {method} hardware decode path"
                break
        else:
            if methods:
                hwdec = "any"
                reason = "Generic HW decode requested (VLC auto-select)"

    if hwdec == "vdpau" and not is_vdpau_ok():
        hwdec = "none"
        reason = "VDPAU probe failed; using CPU fallback"

    profile = {
        "hwdec": hwdec,
        "gpu_available": hwdec != "none",
        "reason": reason,
        "vendors": vendors,
        "methods": methods,
    }
    _GPU_PROFILE_CACHE = profile
    logger.info(
        f"[GPU] vendors={vendors or ['unknown']} methods={methods or ['none']} "
        f"selected_hwdec={hwdec} reason={reason}"
    )
    return dict(profile)


def run_runtime_self_repair(include_gpu_probe=False):
    """
    Best-effort runtime self-repair:
    - recreate runtime directories
    - regenerate/normalize config if broken
    - report missing runtime binaries
    """
    for _dir, _label in [
        (VIDEO_WALLPAPER_DIR, "video"),
        (CONFIG_DIR, "config"),
        (AUTOSTART_DIR, "autostart"),
        (LOCAL_APPLICATIONS_DIR, "applications"),
        (THUMB_CACHE_DIR, "thumb-cache"),
    ]:
        try:
            os.makedirs(_dir, exist_ok=True)
        except OSError as _e:
            logger.warning(f"[Repair] Could not create {_label} dir '{_dir}': {_e}")

    config = ConfigUtil().load()
    try:
        ConfigUtil().save(config)
    except OSError as _e:
        logger.warning(f"[Repair] Could not save config: {_e}")

    repaired_launchers = _repair_user_wallblazer_launchers()
    reverse_cache_cleanup = _cleanup_reverse_cache_dir()
    missing = [cmd for cmd in REQUIRED_RUNTIME_BINARIES if shutil.which(cmd) is None]
    gpu_profile = get_vlc_hwdec_profile(force_refresh=True) if include_gpu_probe else None
    status = {
        "ok": len(missing) == 0,
        "missing_binaries": missing,
        "gpu_profile": gpu_profile,
        "config_path": CONFIG_PATH,
        "video_dir": VIDEO_WALLPAPER_DIR,
        "thumb_cache_dir": THUMB_CACHE_DIR,
        "repaired_launchers": repaired_launchers,
        "reverse_cache_cleanup": reverse_cache_cleanup,
    }
    if missing:
        logger.warning(f"[Repair] Missing runtime binaries: {', '.join(missing)}")
    else:
        logger.info(
            "[Repair] Runtime self-repair completed successfully"
            f" (launchers_fixed={repaired_launchers}, "
            f"reverse_cache_removed={reverse_cache_cleanup.get('removed_files', 0)})"
        )
    return status


def purge_local_app_data():
    """
    Remove persisted local media/user-device state from Wall Blazer config and caches.
    Keeps only safe defaults required for the app to run.
    """
    config_util = ConfigUtil()
    config = config_util.load()

    data_source = config.get(CONFIG_KEY_DATA_SOURCE, {})
    if not isinstance(data_source, dict):
        data_source = {}
    sanitized_data_source = {"Default": ""}
    for key in data_source.keys():
        if isinstance(key, str) and key != "Default":
            sanitized_data_source[key] = ""
    config[CONFIG_KEY_DATA_SOURCE] = sanitized_data_source

    config[CONFIG_KEY_MODE] = MODE_NULL
    config[CONFIG_KEY_PLAYLIST_SELECTION] = []
    config[CONFIG_KEY_PLAYLIST_LIBRARY] = {"Default": []}
    config[CONFIG_KEY_PLAYLIST_ACTIVE] = "Default"
    config[CONFIG_KEY_MONITOR_PLAYLISTS] = {
        key: [] for key in sanitized_data_source.keys()
    }
    config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = {
        key: "Default" for key in sanitized_data_source.keys()
    }

    save_error = None
    try:
        config_util.save(config)
    except OSError as e:
        save_error = str(e)
        logger.warning(f"[Privacy] Could not update config during purge: {e}")

    removed_cache_files = 0
    if os.path.isdir(THUMB_CACHE_DIR):
        for root, _dirs, files in os.walk(THUMB_CACHE_DIR):
            for filename in files:
                file_path = os.path.join(root, filename)
                try:
                    os.remove(file_path)
                    removed_cache_files += 1
                except OSError:
                    pass

    logger.info(
        f"[Privacy] Cleared local app state and removed {removed_cache_files} cached preview files."
    )
    return {
        "ok": save_error is None,
        "error": save_error,
        "config_path": CONFIG_PATH,
        "cache_dir": THUMB_CACHE_DIR,
        "removed_cache_files": removed_cache_files,
    }


def get_gpu_usage_snapshot():
    """
    Return lightweight GPU usage data (best effort).
    Output list item example:
    {
      "vendor": "nvidia",
      "name": "NVIDIA GeForce RTX ...",
      "usage_percent": 31,
      "memory_used_mb": 1200,
      "memory_total_mb": 8192,
      "source": "nvidia-smi"
    }
    """
    snapshot = []

    if shutil.which("nvidia-smi") is not None:
        nvidia_output = _run_text_cmd(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=2,
        )
        if nvidia_output:
            for line in nvidia_output.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 4:
                    continue
                snapshot.append({
                    "vendor": "nvidia",
                    "name": parts[0] or "NVIDIA GPU",
                    "usage_percent": _safe_int(parts[1]),
                    "memory_used_mb": _safe_int(parts[2]),
                    "memory_total_mb": _safe_int(parts[3]),
                    "source": "nvidia-smi",
                })

    has_nvidia_entry = any(item.get("vendor") == "nvidia" for item in snapshot)
    for card_path in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        device_dir = os.path.join(card_path, "device")
        vendor_hex = _read_text_file(os.path.join(device_dir, "vendor")).lower()
        vendor = GPU_VENDOR_MAP.get(vendor_hex, "unknown")
        if vendor == "nvidia" and has_nvidia_entry:
            continue

        usage_percent = None
        for usage_path in (
            os.path.join(device_dir, "gpu_busy_percent"),
            os.path.join(device_dir, "busy_percent"),
        ):
            usage_percent = _safe_int(_read_text_file(usage_path))
            if usage_percent is not None:
                break

        if usage_percent is None:
            continue

        snapshot.append({
            "vendor": vendor,
            "name": os.path.basename(card_path),
            "usage_percent": usage_percent,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "source": "sysfs",
        })

    return snapshot


def setup_autostart(autostart):
    if sys.platform == "win32":
        return
    if is_flatpak():
        """
        Use portal to autostart for Flatpak
        Documentation:
        https://libportal.org/method.Portal.request_background.html
        https://libportal.org/method.Portal.request_background_finish.html 
        """

        gi.require_version("Xdp", "1.0")
        from gi.repository import Xdp
        xdp = Xdp.Portal.new()

        # Request Autostart
        xdp.request_background(
            None,  # parent
            "Autostart Wall Blazer in background",  # reason
            ['wallblazer', '-b'],  # commandline
            Xdp.BackgroundFlags.AUTOSTART if autostart else Xdp.BackgroundFlags.NONE,  # flags
            None,  # cancellable
            lambda portal, result, user_data: logger.debug(
                f"[Utils] autostart={autostart}, request_background sucess={portal.request_background_finish(result)}"),  # callback
            None,  # user_data
        )
        # Bug 4/9: return here so we don't fall through to the non-Flatpak
        # os.makedirs + file write code below, which would unconditionally
        # re-create the desktop file even when autostart=False.
        return
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    logger.debug(
        f"[Utils] autostart={autostart}, path={AUTOSTART_DESKTOP_PATH}")
    if autostart:
        with open(AUTOSTART_DESKTOP_PATH, mode='w') as f:
            if is_flatpak():
                # Write files to the sandbox as well, for the following reasons:
                # (1) So that we know if autostart is enabled by looking the file in sandbox
                # (2) Acts as a fallback in case the portal doesn't work
                f.write(AUTOSTART_DESKTOP_CONTENT_FLATPAK)
            else:
                f.write(AUTOSTART_DESKTOP_CONTENT)
    else:
        if os.path.isfile(AUTOSTART_DESKTOP_PATH):
            os.remove(AUTOSTART_DESKTOP_PATH)


# Opt 2: in-memory cache of paths already confirmed to contain a video stream.
# This avoids re-running ffprobe for the same file on repeated get_video_paths() calls
# (e.g. during playlist ticks or watchdog cycles), which can block for 1-3s per file.
_KNOWN_VIDEO_PATHS: set = set()


def get_video_paths():
    file_list = []
    if not os.path.isdir(VIDEO_WALLPAPER_DIR):
        # Auto-create the directory so users can simply drop videos in it.
        try:
            os.makedirs(VIDEO_WALLPAPER_DIR, exist_ok=True)
            logger.info(f"[Videos] Created wallpaper video directory: {VIDEO_WALLPAPER_DIR}")
        except OSError as e:
            logger.warning(f"[Videos] Could not create video directory '{VIDEO_WALLPAPER_DIR}': {e}")
        return file_list

    def _has_video_stream(filepath):
        # Opt 2: skip ffprobe for already-validated paths.
        if filepath in _KNOWN_VIDEO_PATHS:
            return True
        if shutil.which("ffprobe") is None:
            return False
        try:
            ret = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    filepath,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
            result = ret.returncode == 0 and "video" in (ret.stdout or "").lower()
            if result:
                _KNOWN_VIDEO_PATHS.add(filepath)
            return result
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    for filename in os.listdir(VIDEO_WALLPAPER_DIR):
        filepath = os.path.join(VIDEO_WALLPAPER_DIR, filename)
        if not is_usable_video_path(filepath):
            continue
        file = Gio.file_new_for_path(filepath)
        try:
            info = file.query_info('standard::content-type',
                                   Gio.FileQueryInfoFlags.NONE, None)
            mime_type = (info.get_content_type() or "").lower()
            ext = pathlib.Path(filepath).suffix.lower()
            is_video_mime = ("video" in mime_type) or (mime_type == "application/ogg")
            if is_video_mime or ext in VIDEO_FILE_EXTENSIONS or _has_video_stream(filepath):
                file_list.append(filepath)
        except Exception:
            if (
                pathlib.Path(filepath).suffix.lower() in VIDEO_FILE_EXTENSIONS
                or _has_video_stream(filepath)
            ):
                file_list.append(filepath)
    return sorted(file_list)


def apply_gtk_theme(theme: str):
    """
    Apply GTK dark/light/system theme to the current process.
    theme: 'dark' | 'light' | 'system'
    """
    try:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return
        if theme == "dark":
            settings.props.gtk_application_prefer_dark_theme = True
        else:
            settings.props.gtk_application_prefer_dark_theme = False
    except Exception as e:
        logger.warning(f"[Theme] Could not apply theme '{theme}': {e}")


"""
GNOME extension utils
"""


def gnome_extension_is_enabled(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    info: dict = gnome_ext.GetExtensionInfo(extension_name)
    return info["state"] == 1  # ENABLE = 1


def gnome_extension_set_enable(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    success: bool = gnome_ext.EnableExtension(extension_name)
    return success


def gnome_extension_set_disable(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    success: bool = gnome_ext.DisableExtension(extension_name)
    return success


def gnome_extension_is_installed(extension_name: str):
    gnome_ext = pydbus.SessionBus().get("org.gnome.Shell.Extensions")
    installed: dict = gnome_ext.ListExtensions()
    return extension_name in installed.keys()


def gnome_desktop_icon_workaround():
    """
    Workaround for GNOME desktop icon extensions not displaying the icons on top of Wall Blazer.
    Call this right after the wallpaper is shown.
    """
    if not is_gnome():
        return
    extension_list = ["ding@rastersoft.com",
                      "desktopicons-neo@darkdemon", 
                      "gtk4-ding@smedius.gitlab.com",
                      "zorin-desktop-icons@zorinos.com"
                      ]
    for ext in extension_list:
        # Check if installed and enabled
        if gnome_extension_is_installed(ext) and gnome_extension_is_enabled(ext):
            # Reload the extension
            logger.info(f"[Utils] Apply workaround for {ext}")
            gnome_extension_set_disable(ext)
            gnome_extension_set_enable(ext)


"""
Handlers
"""


class ActiveHandler:
    """
    Handler for monitoring screen lock
    GNOME:
    https://gitlab.gnome.org/GNOME/gnome-shell/-/blob/main/data/dbus-interfaces/org.gnome.ScreenSaver.xml
    Cinamon:
    https://github.com/linuxmint/cinnamon-screensaver/blob/master/libcscreensaver/org.cinnamon.ScreenSaver.xml
    Freedesktop:
    https://github.com/KDE/kscreenlocker/blob/master/dbus/org.freedesktop.ScreenSaver.xml
    """

    def __init__(self, on_active_changed: callable):
        if sys.platform == "win32" or pydbus is None:
            return
        
        self.session_bus = pydbus.SessionBus()
        self.proxies = []
        self.signal_subscriptions = []
        
        screensaver_list = ["org.gnome.ScreenSaver",
                            "org.cinnamon.ScreenSaver",
                            "org.freedesktop.ScreenSaver"]
        for s in screensaver_list:
            try:
                proxy = self.session_bus.get(s)
                # Store proxy reference to prevent garbage collection
                self.proxies.append(proxy)
                subscription = proxy.ActiveChanged.connect(on_active_changed)
                self.signal_subscriptions.append((proxy, subscription))
            except GLib.Error:
                pass

    def cleanup(self):
        """Cleanup signal subscriptions"""
        for proxy, subscription in self.signal_subscriptions:
            try:
                # Note: pydbus doesn't have a direct disconnect method
                # The connection will be cleaned up when the proxy is garbage collected
                pass
            except Exception as e:
                logger.warning(f"[ActiveHandler] Error during cleanup: {e}")
        self.signal_subscriptions.clear()
        self.proxies.clear()


class EndSessionHandler:
    """
    Handler for monitoring end session
    References:
    https://github.com/backloop/gendsession

    PrepareForShutdown() signal from logind is not handled
    https://gitlab.gnome.org/GNOME/gnome-shell/-/issues/787
    """

    def __init__(self, on_end_session: callable):
        self.on_end_session = on_end_session
        if sys.platform == "win32" or pydbus is None:
            return

        try:
            if is_gnome():
                session_bus = pydbus.SessionBus()
                proxy = session_bus.get("org.gnome.SessionManager")
                client_id = proxy.RegisterClient("", "")
                self.session_client = session_bus.get(
                    "org.gnome.SessionManager", client_id)
                self.session_client.QueryEndSession.connect(
                    self.__query_end_session_handler_gnome)
                self.session_client.EndSession.connect(
                    self.__end_session_handler_gnome)
            else:
                system_bus = pydbus.SystemBus()
                proxy = system_bus.get(".login1")
                proxy.PrepareForShutdown.connect(self.__end_session_handler)
        except Exception as e:
            logger.warning(f"[EndSessionHandler] Could not connect to D-Bus: {e}. End-session monitoring disabled.")

    def __end_session_response_gnome(self, ok=True):
        if ok:
            self.session_client.EndSessionResponse(True, "")
        else:
            self.session_client.EndSessionResponse(False, "Not ready")

    def __query_end_session_handler_gnome(self, flags):
        # Ignore flags, always agree on the QueryEndSesion
        self.__end_session_response_gnome(True)

    def __end_session_handler_gnome(self, flags):
        logger.debug("[EndSessionHandler] called")
        self.on_end_session()
        self.__end_session_response_gnome(True)

    def __end_session_handler(self, *_):
        logger.debug("[EndSessionHandler] called")
        self.on_end_session()


class WindowHandler:
    """
    Handler for monitoring window events (maximized and fullscreen mode) for X11
    """

    def __init__(self, on_window_state_changed: callable):
        self.on_window_state_changed = on_window_state_changed
        self.screen = Wnck.Screen.get_default()
        self.screen.force_update()
        
        # Store signal handler IDs for cleanup
        self.signal_handlers = []
        self.window_signal_handlers = {}
        
        # Connect screen signals and store handler IDs
        handler_id = self.screen.connect("window-opened", self.window_opened, None)
        self.signal_handlers.append((self.screen, handler_id))
        
        handler_id = self.screen.connect("window-closed", self.eval, None)
        self.signal_handlers.append((self.screen, handler_id))
        
        handler_id = self.screen.connect("active-workspace-changed", self.eval, None)
        self.signal_handlers.append((self.screen, handler_id))
        
        # Connect to existing windows
        for window in self.screen.get_windows():
            self._connect_window(window)

        self.prev_state = None
        # Initial check
        self.eval()

    def _connect_window(self, window):
        """Connect to a window and store the handler ID"""
        if window not in self.window_signal_handlers:
            handler_id = window.connect("state-changed", self.eval, None)
            self.window_signal_handlers[window] = handler_id

    def window_opened(self, screen, window, _):
        self._connect_window(window)

    def eval(self, *args):
        # TODO: #28 (Wallpaper stops animating on other monitor when app maximized on other)
        is_changed = False

        is_any_maximized, is_any_fullscreen = False, False
        for window in self.screen.get_windows():
            base_state = not Wnck.Window.is_minimized(window) and \
                Wnck.Window.is_on_workspace(
                    window, self.screen.get_active_workspace())
            window_name, is_maximized, is_fullscreen = window.get_name(), \
                Wnck.Window.is_maximized(window) and base_state, \
                Wnck.Window.is_fullscreen(window) and base_state
            if is_maximized is True:
                is_any_maximized = True
            if is_fullscreen is True:
                is_any_fullscreen = True

        cur_state = {"is_any_maximized": is_any_maximized,
                     "is_any_fullscreen": is_any_fullscreen}
        if self.prev_state is None or self.prev_state != cur_state:
            is_changed = True
            self.prev_state = cur_state

        if is_changed:
            self.on_window_state_changed(
                {"is_any_maximized": is_any_maximized, "is_any_fullscreen": is_any_fullscreen})
            logger.debug(f"[WindowHandler] {cur_state}")

    def cleanup(self):
        """Cleanup all signal handlers to prevent memory leaks"""
        # Disconnect screen signals
        for obj, handler_id in self.signal_handlers:
            try:
                obj.disconnect(handler_id)
            except Exception as e:
                logger.warning(f"[WindowHandler] Error disconnecting screen signal: {e}")
        self.signal_handlers.clear()
        
        # Disconnect window signals
        for window, handler_id in self.window_signal_handlers.items():
            try:
                window.disconnect(handler_id)
            except Exception as e:
                logger.warning(f"[WindowHandler] Error disconnecting window signal: {e}")
        self.window_signal_handlers.clear()


# class WindowHandlerGnome:
#     """
#     Handler for monitoring window events for Gnome only
#     TODO: This is broken due to a change in GNOME =(
#     https://gitlab.gnome.org/GNOME/gnome-shell/-/commit/7298ee23e91b756c7009b4d7687dfd8673856f8b

#     TLDR, there is no way to monitor window events in Wayland, unless we use an Shell extension.
#     To bypass, execute the below line in looking glass (Alt+F2 `lg`)
#     `global.context.unsafe_mode = true`
#     """

#     def __init__(self, on_window_state_changed: callable):
#         self.on_window_state_changed = on_window_state_changed
#         self.gnome_shell = pydbus.SessionBus().get("org.gnome.Shell")
#         self.prev_state = None
#         display = Gdk.Display.get_default()
#         self.num_monitor = display.get_n_monitors()
#         GLib.timeout_add(500, self.eval)

#     def eval(self):
#         is_changed = False

#         ret1, workspace = self.gnome_shell.Eval("""
#                         global.workspace_manager.get_active_workspace_index()
#                         """)
#         ret2 = False
#         maximized = []
#         for monitor in range(self.num_monitor):
#             ret2, temp = self.gnome_shell.Eval(f"""
#                             var window_list = global.get_window_actors().find(window =>
#                                 window.meta_window.maximized_horizontally &
#                                 window.meta_window.maximized_vertically &
#                                 !window.meta_window.minimized &
#                                 window.meta_window.get_workspace().workspace_index == {workspace} &
#                                 window.meta_window.get_monitor() == {monitor}
#                             );
#                             window_list
#                             """)
#             maximized.append(temp != "")
#         # Every monitors have a maximized window?
#         maximized = all(maximized)

#         ret3 = False
#         fullscreen = []
#         for monitor in range(self.num_monitor):
#             ret3, temp = self.gnome_shell.Eval(f"""
#                             var window_list = global.get_window_actors().find(window =>
#                     window.meta_window.is_fullscreen() &
#                     !window.meta_window.minimized &
#                     window.meta_window.get_workspace().workspace_index == {workspace} &
#                     window.meta_window.get_monitor() == {monitor}
#                 );
#                 window_list
#                 """)
#             fullscreen.append(temp != "")
#         # Every monitors have a fullscreen window?
#         fullscreen = all(fullscreen)

#         if not all([ret1, ret2, ret3]):
#             logging.error(
#                 "[WindowHandlerGnome] Cannot communicate with Gnome Shell!")

#         cur_state = {'is_any_maximized': maximized,
#                      'is_any_fullscreen': fullscreen}
#         if self.prev_state is None or self.prev_state != cur_state:
#             is_changed = True
#             self.prev_state = cur_state

#         if is_changed:
#             self.on_window_state_changed(
#                 {"is_any_maximized": maximized, "is_any_fullscreen": fullscreen})
#             logger.debug(f"[WindowHandlerGnome] {cur_state}")
#         return True


class ConfigUtil:
    def generate_template(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
        except OSError as e:
            logger.warning(f"[Config] Could not create config dir '{CONFIG_DIR}': {e}")
            return
        try:
            self.save(CONFIG_TEMPLATE)
        except OSError as e:
            logger.warning(f"[Config] Could not write config template: {e}")

    @staticmethod
    def _check(config: dict):
        """Check if the config is valid"""
        is_all_keys_match = all(key in config for key in CONFIG_TEMPLATE)
        is_version_match = config.get("version") == CONFIG_VERSION
        return is_all_keys_match and is_version_match

    def _invalid(self):
        logger.debug(f"[Config] Invalid. A new config will be generated.")
        self.generate_template()
        return CONFIG_TEMPLATE

    @staticmethod
    def _normalize_playlist_items(items):
        if not isinstance(items, list):
            return []
        seen = set()
        normalized = []
        for item in items:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value or value in seen or is_transient_video_path(value):
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _unique_playlist_name(base_name, existing_playlists: dict):
        base = str(base_name).strip() or "Playlist"
        if base not in existing_playlists:
            return base
        i = 2
        while f"{base} {i}" in existing_playlists:
            i += 1
        return f"{base} {i}"

    @staticmethod
    def _clamp_number(value, default, minimum, maximum):
        parsed = _safe_float(value)
        if parsed is None:
            return default
        return max(minimum, min(maximum, parsed))

    def _normalize_video_profile(self, config: dict):
        changed = False

        fit_mode = config.get(CONFIG_KEY_VIDEO_FIT_MODE, CONFIG_TEMPLATE[CONFIG_KEY_VIDEO_FIT_MODE])
        if fit_mode not in {"cover", "contain", "stretch"}:
            fit_mode = CONFIG_TEMPLATE[CONFIG_KEY_VIDEO_FIT_MODE]
            changed = True
        if config.get(CONFIG_KEY_VIDEO_FIT_MODE) != fit_mode:
            config[CONFIG_KEY_VIDEO_FIT_MODE] = fit_mode
            changed = True

        speed_specs = {
            CONFIG_KEY_PLAYBACK_SPEED_SINGLE: CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_SINGLE],
            CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST: CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST],
            CONFIG_KEY_PLAYBACK_SPEED_REVERSE: CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_REVERSE],
        }
        for key, default in speed_specs.items():
            normalized = self._clamp_number(config.get(key), default, 0.25, 4.0)
            normalized = round(normalized / 0.25) * 0.25
            if config.get(key) != normalized:
                config[key] = normalized
                changed = True

        raw_adjustments = config.get(CONFIG_KEY_VIDEO_ADJUSTMENTS, {})
        if not isinstance(raw_adjustments, dict):
            raw_adjustments = {}
            changed = True

        adjustment_specs = {
            "brightness": (DEFAULT_VIDEO_ADJUSTMENTS["brightness"], 0.2, 3.0),
            "contrast": (DEFAULT_VIDEO_ADJUSTMENTS["contrast"], 0.2, 3.0),
            "saturation": (DEFAULT_VIDEO_ADJUSTMENTS["saturation"], 0.0, 4.0),
            "gamma": (DEFAULT_VIDEO_ADJUSTMENTS["gamma"], 0.2, 4.0),
            "hue": (DEFAULT_VIDEO_ADJUSTMENTS["hue"], -180.0, 180.0),
            "red": (DEFAULT_VIDEO_ADJUSTMENTS["red"], -1.0, 1.0),
            "green": (DEFAULT_VIDEO_ADJUSTMENTS["green"], -1.0, 1.0),
            "blue": (DEFAULT_VIDEO_ADJUSTMENTS["blue"], -1.0, 1.0),
            "yellow": (DEFAULT_VIDEO_ADJUSTMENTS["yellow"], -1.0, 1.0),
            "cyan": (DEFAULT_VIDEO_ADJUSTMENTS["cyan"], -1.0, 1.0),
            "magenta": (DEFAULT_VIDEO_ADJUSTMENTS["magenta"], -1.0, 1.0),
        }
        normalized_adjustments = {}
        for key, (default, minimum, maximum) in adjustment_specs.items():
            normalized = self._clamp_number(raw_adjustments.get(key), default, minimum, maximum)
            if key == "hue":
                normalized = round(normalized)
            else:
                normalized = round(normalized, 3)
            normalized_adjustments[key] = normalized

        if config.get(CONFIG_KEY_VIDEO_ADJUSTMENTS) != normalized_adjustments:
            config[CONFIG_KEY_VIDEO_ADJUSTMENTS] = normalized_adjustments
            changed = True

        return changed

    def _normalize_playlist_config(self, config: dict):
        changed = False
        library = config.get(CONFIG_KEY_PLAYLIST_LIBRARY)
        normalized_library = {}
        if isinstance(library, dict):
            for raw_name, raw_items in library.items():
                name = str(raw_name).strip()
                if not name:
                    continue
                normalized_library[name] = self._normalize_playlist_items(raw_items)

        if not normalized_library:
            normalized_library = {
                "Default": self._normalize_playlist_items(
                    config.get(CONFIG_KEY_PLAYLIST_SELECTION, [])
                )
            }
            changed = True

        active_name = config.get(CONFIG_KEY_PLAYLIST_ACTIVE, "Default")
        if not isinstance(active_name, str) or active_name not in normalized_library:
            active_name = next(iter(normalized_library.keys()))
            changed = True

        monitor_names = []
        data_source = config.get(CONFIG_KEY_DATA_SOURCE, {})
        if isinstance(data_source, dict):
            for monitor_name in data_source.keys():
                if isinstance(monitor_name, str):
                    monitor_names.append(monitor_name)
        if "Default" not in monitor_names:
            monitor_names.append("Default")

        monitor_playlists = config.get(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        if not isinstance(monitor_playlists, dict):
            monitor_playlists = {}
            changed = True

        assignments = config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        if not isinstance(assignments, dict):
            assignments = {}
            changed = True

        normalized_assignments = {}
        for monitor_name in monitor_names:
            assigned_playlist = assignments.get(monitor_name)
            if not isinstance(assigned_playlist, str) or assigned_playlist not in normalized_library:
                legacy_items = self._normalize_playlist_items(monitor_playlists.get(monitor_name, []))
                assigned_playlist = None
                if legacy_items:
                    for playlist_name, playlist_items in normalized_library.items():
                        if playlist_items == legacy_items:
                            assigned_playlist = playlist_name
                            break
                    if assigned_playlist is None:
                        preferred_name = "Default" if monitor_name == "Default" else f"{monitor_name} Playlist"
                        if preferred_name in normalized_library and normalized_library.get(preferred_name) != legacy_items:
                            preferred_name = self._unique_playlist_name(preferred_name, normalized_library)
                        normalized_library[preferred_name] = legacy_items
                        assigned_playlist = preferred_name
                        changed = True
                if assigned_playlist is None:
                    assigned_playlist = active_name
                changed = True
            normalized_assignments[monitor_name] = assigned_playlist

        for monitor_name, assigned_playlist in assignments.items():
            if monitor_name in normalized_assignments:
                continue
            if not isinstance(monitor_name, str):
                continue
            if isinstance(assigned_playlist, str) and assigned_playlist in normalized_library:
                normalized_assignments[monitor_name] = assigned_playlist

        derived_monitor_playlists = {}
        for monitor_name, playlist_name in normalized_assignments.items():
            derived_monitor_playlists[monitor_name] = list(
                normalized_library.get(playlist_name, [])
            )

        active_items = normalized_library.get(active_name, [])
        if config.get(CONFIG_KEY_PLAYLIST_SELECTION) != active_items:
            config[CONFIG_KEY_PLAYLIST_SELECTION] = list(active_items)
            changed = True

        if config.get(CONFIG_KEY_PLAYLIST_LIBRARY) != normalized_library:
            config[CONFIG_KEY_PLAYLIST_LIBRARY] = normalized_library
            changed = True

        if config.get(CONFIG_KEY_PLAYLIST_ACTIVE) != active_name:
            config[CONFIG_KEY_PLAYLIST_ACTIVE] = active_name
            changed = True

        if config.get(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS) != normalized_assignments:
            config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = normalized_assignments
            changed = True

        if config.get(CONFIG_KEY_MONITOR_PLAYLISTS) != derived_monitor_playlists:
            config[CONFIG_KEY_MONITOR_PLAYLISTS] = derived_monitor_playlists
            changed = True

        # Normalize reverse-playback items per playlist
        reverse_items = config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS, {})
        if not isinstance(reverse_items, dict):
            reverse_items = {}
            changed = True
        normalized_reverse = {}
        for playlist_name, playlist_items in normalized_library.items():
            items = reverse_items.get(playlist_name, [])
            if not isinstance(items, list):
                items = []
                changed = True
            playlist_set = set(playlist_items)
            filtered = [
                item for item in self._normalize_playlist_items(items)
                if item in playlist_set
            ]
            if filtered:
                normalized_reverse[playlist_name] = filtered
            if filtered != items:
                changed = True
        if set(reverse_items.keys()) - set(normalized_library.keys()):
            changed = True
        if config.get(CONFIG_KEY_REVERSE_PLAYLIST_ITEMS) != normalized_reverse:
            config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = normalized_reverse
            changed = True

        return changed
        
    def _migrateV3To4(self, config: dict):
        logger.debug(f"[Config] Migration from version 3 to 4.")
        curr_data_source = config['data_source']
        config['data_source'] = CONFIG_TEMPLATE[CONFIG_KEY_DATA_SOURCE]
        config['data_source']['Default'] = curr_data_source
        config['is_pause_when_maximized'] = config["is_detect_maximized"]
        del config["is_detect_maximized"]
        config['is_mute_when_maximized'] = CONFIG_TEMPLATE[CONFIG_KEY_MUTE_WHEN_MAXIMIZED]
        config['version'] = 4
        # save config file
        self.save(config)
        
    def _checkMissingMonitors(self, old_config: dict, template: dict):
        # Extract the monitors from both configurations
        old_monitors = old_config.get("data_source", {}).keys()
        template_monitors = template.get("data_source", {}).keys()
        # Find monitors in the template that are not in the old configuration
        missing_monitors = set(template_monitors) - set(old_monitors)
        if len(missing_monitors) > 0:
            logger.warning(f"[Config] There are missing {len(missing_monitors)} monitors in config. Creating default one")
            self._createMissingMonitors(missing_monitors, old_config)
    
    def _createMissingMonitors(self, keys: set, config: dict):
        # we will set to Default new monitor sources
        monitor_playlists = config.setdefault(CONFIG_KEY_MONITOR_PLAYLISTS, {})
        monitor_assignments = config.setdefault(CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS, {})
        playlist_library = config.setdefault(CONFIG_KEY_PLAYLIST_LIBRARY, {"Default": []})
        active_playlist = config.get(CONFIG_KEY_PLAYLIST_ACTIVE, "Default")
        if active_playlist not in playlist_library:
            active_playlist = next(iter(playlist_library.keys()))
        for key in keys:
            config['data_source'][key] = config['data_source']['Default']
            if key not in monitor_assignments:
                monitor_assignments[key] = active_playlist
            assigned = monitor_assignments.get(key, active_playlist)
            monitor_playlists[key] = list(playlist_library.get(assigned, []))
        self.save(config)
        
    def _repairVideoSources(self, config: dict):
        mode = config.get(CONFIG_KEY_MODE)
        data_source = config.get(CONFIG_KEY_DATA_SOURCE, {})
        if mode != MODE_VIDEO or not isinstance(data_source, dict):
            return False

        changed = False
        fallback_source = ""

        default_source = data_source.get("Default", "")
        if is_usable_video_path(default_source):
            fallback_source = default_source
        else:
            for value in data_source.values():
                if is_usable_video_path(value):
                    fallback_source = value
                    break
            if not fallback_source:
                available_videos = get_video_paths()
                fallback_source = available_videos[0] if available_videos else ""
            if data_source.get("Default", "") != fallback_source:
                if fallback_source:
                    logger.warning(
                        "[Config] Default source is empty or invalid. Falling back to a valid library video."
                    )
                else:
                    logger.warning(
                        "[Config] No valid video sources were found. Clearing invalid wallpaper paths."
                    )
                data_source["Default"] = fallback_source
                changed = True

        for monitor_name, current_source in list(data_source.items()):
            if monitor_name == "Default":
                continue
            if not isinstance(current_source, str):
                current_source = ""
            if is_usable_video_path(current_source):
                continue
            replacement = fallback_source or ""
            if data_source.get(monitor_name, "") != replacement:
                data_source[monitor_name] = replacement
                changed = True

        if changed:
            config[CONFIG_KEY_DATA_SOURCE] = data_source

        if not fallback_source and config.get(CONFIG_KEY_MODE) == MODE_VIDEO:
            config[CONFIG_KEY_MODE] = MODE_NULL
            changed = True
            logger.warning(
                "[Config] Video mode has no valid local sources. Switching to MODE_NULL."
            )

        return changed
                    
    def load(self):
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                json_str = f.read()
                try:
                    config = json.loads(json_str)
                    # migration: versions <= 3 need data_source restructure
                    if config.get("version", 0) <= 3 and CONFIG_VERSION >= 4:
                        self._migrateV3To4(config)
                    # migration v4 -> v5: add playlist + theme keys
                    if config.get("version", 0) == 4 and CONFIG_VERSION >= 5:
                        config[CONFIG_KEY_PLAYLIST] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST]
                        config[CONFIG_KEY_PLAYLIST_INTERVAL] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST_INTERVAL]
                        config[CONFIG_KEY_PLAYLIST_SHUFFLE] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST_SHUFFLE]
                        config[CONFIG_KEY_THEME] = CONFIG_TEMPLATE[CONFIG_KEY_THEME]
                        config["version"] = 5
                        self.save(config)
                    # migration v5 -> v6: add explicit playlist selection
                    if config.get("version", 0) == 5 and CONFIG_VERSION >= 6:
                        config[CONFIG_KEY_PLAYLIST_SELECTION] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYLIST_SELECTION]
                        config["version"] = 6
                        self.save(config)
                    # migration v6 -> v7: add named playlist library + active playlist key
                    if config.get("version", 0) == 6 and CONFIG_VERSION >= 7:
                        config[CONFIG_KEY_PLAYLIST_LIBRARY] = {
                            "Default": self._normalize_playlist_items(
                                config.get(CONFIG_KEY_PLAYLIST_SELECTION, [])
                            )
                        }
                        config[CONFIG_KEY_PLAYLIST_ACTIVE] = "Default"
                        config["version"] = 7
                        self.save(config)
                    # migration v7 → v8: add per-monitor playlists
                    if config.get("version", 0) == 7 and CONFIG_VERSION >= 8:
                        # Build initial monitor_playlists from existing playlist_selection
                        existing_selection = self._normalize_playlist_items(
                            config.get(CONFIG_KEY_PLAYLIST_SELECTION, [])
                        )
                        monitor_playlists = {}
                        data_src = config.get(CONFIG_KEY_DATA_SOURCE, {})
                        for monitor_name in data_src:
                            # Give every monitor the existing playlist selection as a starting point
                            monitor_playlists[monitor_name] = list(existing_selection)
                        config[CONFIG_KEY_MONITOR_PLAYLISTS] = monitor_playlists
                        config["version"] = 8
                        self.save(config)
                    # migration v8 -> v9: add per-monitor playlist assignments
                    if config.get("version", 0) == 8 and CONFIG_VERSION >= 9:
                        config[CONFIG_KEY_MONITOR_PLAYLIST_ASSIGNMENTS] = {}
                        config["version"] = 9
                        self._normalize_playlist_config(config)
                        self.save(config)
                    # migration v9 -> v10: add reverse playback controls
                    if config.get("version", 0) == 9 and CONFIG_VERSION >= 10:
                        config[CONFIG_KEY_REVERSE_SINGLE] = CONFIG_TEMPLATE[CONFIG_KEY_REVERSE_SINGLE]
                        config[CONFIG_KEY_REVERSE_PLAYLIST] = CONFIG_TEMPLATE[CONFIG_KEY_REVERSE_PLAYLIST]
                        config[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS] = CONFIG_TEMPLATE[CONFIG_KEY_REVERSE_PLAYLIST_ITEMS]
                        config["version"] = 10
                        self.save(config)
                    # migration v10 -> v11: add fit/rate/video adjustment controls
                    if config.get("version", 0) == 10 and CONFIG_VERSION >= 11:
                        config[CONFIG_KEY_VIDEO_FIT_MODE] = CONFIG_TEMPLATE[CONFIG_KEY_VIDEO_FIT_MODE]
                        config[CONFIG_KEY_PLAYBACK_SPEED_SINGLE] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_SINGLE]
                        config[CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_PLAYLIST]
                        config[CONFIG_KEY_PLAYBACK_SPEED_REVERSE] = CONFIG_TEMPLATE[CONFIG_KEY_PLAYBACK_SPEED_REVERSE]
                        config[CONFIG_KEY_VIDEO_ADJUSTMENTS] = dict(CONFIG_TEMPLATE[CONFIG_KEY_VIDEO_ADJUSTMENTS])
                        config["version"] = 11
                        self.save(config)
                    if self._normalize_playlist_config(config):
                        self.save(config)
                    if self._normalize_video_profile(config):
                        self.save(config)
                    if self._repairVideoSources(config):
                        self.save(config)
                    self._checkMissingMonitors(config, CONFIG_TEMPLATE)
                    if self._check(config):
                        summary = _redacted_config_summary(config)
                        logger.debug(f"[Config] Loaded {CONFIG_PATH} summary={summary}")
                        return config
                except json.decoder.JSONDecodeError:
                    logger.debug("[Config] JSONDecodeError")
        return self._invalid()

    def save(self, config):
        old_config = None
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                json_str = f.read()
                try:
                    old_config = json.loads(json_str)
                    if not self._check(old_config):
                        old_config = None
                except json.decoder.JSONDecodeError:
                    old_config = None
        # Skip if the config is identical
        if old_config == config:
            return
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json_str = json.dumps(config, indent=3)
                print(json_str, file=f)
                summary = _redacted_config_summary(config)
                logger.debug(f"[Config] Saved {CONFIG_PATH} summary={summary}")
        except OSError as e:
            logger.warning(f"[Config] Could not save config to '{CONFIG_PATH}': {e}")
