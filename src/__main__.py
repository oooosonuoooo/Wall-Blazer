import argparse
import logging
import os
import sys

os.environ["NO_AT_BRIDGE"] = "1"

MODULE_DIR = os.path.abspath(os.path.dirname(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

# TODO: Is there any way to make these imports look better?
try:
    from commons import *
    from utils import (
        is_gnome, is_wayland, is_nvidia_proprietary, is_vdpau_ok, is_flatpak,
        run_runtime_self_repair, purge_local_app_data,
    )
    import server
except (ModuleNotFoundError, ImportError):
    # These are imports for installed/Flatpak mode
    from wallblazer.commons import *
    from wallblazer.utils import (
        is_gnome, is_wayland, is_nvidia_proprietary, is_vdpau_ok, is_flatpak,
        run_runtime_self_repair, purge_local_app_data,
    )
    from wallblazer import server

logger = logging.getLogger(LOGGER_NAME)


def _configure_logging(args):
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, force=True)
        return
    if args.repair or args.purge_device_data:
        logging.basicConfig(level=logging.INFO, force=True)


def _resolve_runtime_paths(pkgdatadir, localedir):
    candidates = []
    seen = set()

    def _add_candidate(path):
        if not isinstance(path, str):
            return
        normalized = os.path.normpath(path)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    _add_candidate(pkgdatadir)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        _add_candidate(getattr(sys, "_MEIPASS", ""))
        _add_candidate(exe_dir)
        _add_candidate(os.path.join(exe_dir, "_internal"))
        _add_candidate(os.path.join(exe_dir, "src"))
        _add_candidate(os.path.join(exe_dir, "_internal", "src"))

    repo_root = os.path.abspath(os.path.join(MODULE_DIR, ".."))
    _add_candidate(os.path.join(repo_root, "build", "src"))
    _add_candidate(MODULE_DIR)

    for candidate in candidates:
        for resource_dir in (candidate, os.path.join(candidate, "src")):
            if os.path.isfile(os.path.join(resource_dir, "wallblazer.gresource")):
                return resource_dir, localedir
    return pkgdatadir, localedir


# TODO: Add locale support
def main(version="devel", pkgdatadir="/app/share/wallblazer", localedir="/app/share/locale"):
    pkgdatadir, localedir = _resolve_runtime_paths(pkgdatadir, localedir)
    # Suppress VLC Log
    os.environ["VLC_VERBOSE"] = "-1"

    parser = argparse.ArgumentParser(description=f"Wall Blazer v{version}")
    parser.add_argument("-p", "--pause", dest="p", type=int, default=0,
                        help="Add pause before launching Wall Blazer. [sec]")
    parser.add_argument("-b", "--background", action="store_true", help="Launch only the live wallpaper.")
    parser.add_argument("-d", "--debug", action="store_true", help="Print debug messages.")
    parser.add_argument("-r", "--reset", action="store_true", help="Reset user configuration.")
    parser.add_argument("--repair", action="store_true",
                        help="Run runtime health checks and self-repair tasks, then exit.")
    parser.add_argument("--purge-device-data", action="store_true",
                        help="Clear local file paths, playlist state, and cached previews from app data.")
    # Internal flag: launch only the GUI window (called by server via subprocess.Popen)
    parser.add_argument("--gui-only", nargs=2, metavar=("PKGDATADIR", "LOCALEDIR"),
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    _configure_logging(args)

    # --gui-only mode: just open the GTK control panel, no server logic
    if args.gui_only:
        _pkgdatadir, _localedir = args.gui_only
        if sys.platform != "win32":
            pass  # Do not force x11 for gui-only
        os.environ.setdefault("VLC_VERBOSE", "-1")
        sys.argv = [sys.argv[0]]
        try:
            from gui.control import main as gui_main
        except (ModuleNotFoundError, ImportError):
            from wallblazer.gui.control import main as gui_main
        gui_main(version, _pkgdatadir, _localedir)
        return

    if args.repair:
        status = run_runtime_self_repair(include_gpu_probe=True)
        missing = status.get("missing_binaries", [])
        if missing:
            logger.error(
                "[Repair] Missing system dependencies: "
                + ", ".join(missing)
                + ". Run install.sh --repair to reinstall dependencies."
            )
            raise SystemExit(2)
        logger.info(
            f"[Repair] OK. config={status.get('config_path')} "
            f"video_dir={status.get('video_dir')} gpu={status.get('gpu_profile', {}).get('hwdec')}"
        )
        return

    if args.purge_device_data:
        status = purge_local_app_data()
        if not status.get("ok", False):
            logger.error(
                f"[Privacy] Could not fully clear local state: {status.get('error')}"
            )
            raise SystemExit(2)
        logger.info(
            f"[Privacy] Device-related local state cleared. "
            f"config={status.get('config_path')} cache={status.get('cache_dir')} "
            f"removed_cache_files={status.get('removed_cache_files')}"
        )
        return

    # Log system information
    sys_info = []
    sys_info.append("--- System information ---")
    sys_info.append(f"is_gnome = {is_gnome()}")
    sys_info.append(f"is_wayland = {is_wayland()}")
    sys_info.append(f"is_nvidia_proprietary = {is_nvidia_proprietary()}")
    sys_info.append(f"is_vdpau_ok = {is_vdpau_ok()}")
    sys_info.append(f"is_flatpak = {is_flatpak()}")
    sys_info.append("--------------------------")
    sys_info_str = "\n".join(sys_info)
    logger.info(f"Wall Blazer v{version}\n{sys_info_str}")
    logger.info(f"[Args] {vars(args)}")

    # Make Wall Blazer runtime folders and normalize config
    run_runtime_self_repair()

    # Clear sys.argv as it has influence to the Gtk.Application
    sys.argv = [sys.argv[0]]
    server.main(version, pkgdatadir, localedir, args)


if __name__ == "__main__":
    main()
