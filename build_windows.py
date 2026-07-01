from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DIST_DIR = PROJECT_ROOT / "dist" / "Wall-Blazer"
BUILD_DIR = PROJECT_ROOT / "build"
TEMP_HOOK = BUILD_DIR / "pyi_rth_wallblazer.py"

DEFAULT_VLC_PATHS = [
    Path(r"C:\Program Files\VideoLAN\VLC"),
    Path(r"C:\Program Files (x86)\VideoLAN\VLC"),
]

DEFAULT_GTK_RUNTIME_ROOTS = [
    Path(os.environ.get("MSYSTEM_PREFIX", "")),
    Path(os.environ.get("GTK_RUNTIME_ROOT", "")),
    Path(r"C:\msys64\ucrt64"),
    Path(r"C:\msys64\mingw64"),
]


def _existing_path(paths):
    for path in paths:
        if not path:
            continue
        if str(path) in {"", "."}:
            continue
        if path.exists():
            return path
    return None


def find_vlc_dir():
    env_path = os.environ.get("VLC_DIR")
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return candidate
    return _existing_path(DEFAULT_VLC_PATHS)


def find_gtk_runtime_root():
    root = _existing_path(DEFAULT_GTK_RUNTIME_ROOTS)
    if root is None:
        raise FileNotFoundError(
            "GTK runtime not found. Install MSYS2 UCRT64 first or set GTK_RUNTIME_ROOT."
        )
    if not (root / "bin").exists():
        raise FileNotFoundError(f"GTK runtime is missing bin/: {root}")
    if not (root / "lib" / "girepository-1.0").exists():
        raise FileNotFoundError(f"GTK runtime is missing typelibs: {root}")
    return root


def write_runtime_hook():
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_HOOK.write_text(
        "\n".join(
            [
                "import os",
                "import sys",
                "",
                "base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))",
                "gtk_root = os.path.join(base, 'gtk-runtime')",
                "gtk_bin = os.path.join(gtk_root, 'bin')",
                "gtk_typelib = os.path.join(gtk_root, 'lib', 'girepository-1.0')",
                "gtk_schemas = os.path.join(gtk_root, 'share', 'glib-2.0', 'schemas')",
                "",
                "extra_path = [p for p in (gtk_bin, base) if os.path.isdir(p)]",
                "if extra_path:",
                "    os.environ['PATH'] = os.pathsep.join(extra_path + [os.environ.get('PATH', '')])",
                "if os.path.isdir(gtk_bin):",
                "    os.environ.setdefault('PYGI_DLL_PATH', gtk_bin)",
                "if os.path.isdir(gtk_typelib):",
                "    os.environ.setdefault('GI_TYPELIB_PATH', gtk_typelib)",
                "if os.path.isdir(gtk_schemas):",
                "    os.environ.setdefault('GSETTINGS_SCHEMA_DIR', gtk_schemas)",
                "os.environ.setdefault('WALLBLAZER_LOCAL_IPC', '1')",
            ]
        ),
        encoding="utf-8",
    )
    return TEMP_HOOK


def copy_tree(src: Path, dst: Path):
    if not src.exists():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def copy_gtk_runtime(runtime_root: Path):
    target_root = DIST_DIR / "gtk-runtime"
    copy_tree(runtime_root / "lib" / "girepository-1.0", target_root / "lib" / "girepository-1.0")
    copy_tree(runtime_root / "share" / "glib-2.0" / "schemas", target_root / "share" / "glib-2.0" / "schemas")

    icons_dir = runtime_root / "share" / "icons"
    if icons_dir.exists():
        copy_tree(icons_dir, target_root / "share" / "icons")

    themes_dir = runtime_root / "share" / "themes"
    if themes_dir.exists():
        copy_tree(themes_dir, target_root / "share" / "themes")

    (target_root / "bin").mkdir(parents=True, exist_ok=True)
    for dll in (runtime_root / "bin").glob("*.dll"):
        shutil.copy2(dll, target_root / "bin" / dll.name)


def copy_vlc_runtime(vlc_dir: Path | None):
    if vlc_dir is None:
        print("[WARN] VLC runtime not found. The build will complete, but VLC DLLs are not bundled.")
        return

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    for dll in vlc_dir.glob("*.dll"):
        shutil.copy2(dll, DIST_DIR / dll.name)

    plugins_src = vlc_dir / "plugins"
    if plugins_src.exists():
        copy_tree(plugins_src, DIST_DIR / "plugins")


def ensure_windows_icon():
    icon_path = PROJECT_ROOT / "assets" / "wallblazer.ico"
    if icon_path.exists():
        return icon_path

    generated_icon = BUILD_DIR / "wallblazer.generated.ico"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw

        canvas_size = 256
        image = Image.new("RGBA", (canvas_size, canvas_size), (18, 38, 68, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (20, 20, canvas_size - 20, canvas_size - 20),
            radius=52,
            fill=(33, 78, 146, 255),
            outline=(180, 216, 255, 255),
            width=8,
        )
        draw.polygon(
            [(88, 70), (132, 186), (168, 132), (196, 186)],
            fill=(240, 248, 255, 255),
        )
        image.save(
            generated_icon,
            format="ICO",
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
        )
        print(f"[INFO] Generated fallback icon: {generated_icon}")
        return generated_icon
    except Exception as exc:
        print(f"[WARN] Could not generate fallback icon: {exc}")
    return None


def _find_glib_compile_resources(gtk_root: Path):
    candidates = [
        shutil.which("glib-compile-resources"),
        str(gtk_root / "bin" / "glib-compile-resources.exe"),
        str(gtk_root / "bin" / "glib-compile-resources"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isfile(candidate):
            return candidate
    return None


def build_gresource(gtk_root: Path):
    source_xml = PROJECT_ROOT / "src" / "wallblazer.gresource.xml"
    source_dir = PROJECT_ROOT / "src"
    output = BUILD_DIR / "wallblazer.gresource"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    if not source_xml.exists():
        return None

    compiler = _find_glib_compile_resources(gtk_root)
    if not compiler:
        fallback = PROJECT_ROOT / "build" / "src" / "wallblazer.gresource"
        if fallback.exists():
            return fallback
        print("[WARN] glib-compile-resources was not found; continuing without bundled gresource.")
        return None

    subprocess.check_call(
        [
            compiler,
            "--target",
            str(output),
            "--sourcedir",
            str(source_dir),
            str(source_xml),
        ],
        cwd=PROJECT_ROOT,
    )
    return output if output.exists() else None


def build():
    if os.name != "nt":
        print("[INFO] build_windows.py is intended to run on Windows/MSYS2.")

    gtk_root = find_gtk_runtime_root()
    vlc_dir = find_vlc_dir()
    hook_path = write_runtime_hook()
    icon_path = ensure_windows_icon()
    gresource_path = build_gresource(gtk_root)

    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        "Wall-Blazer",
        "--windowed",
        "--paths",
        str(PROJECT_ROOT / "src"),
        "--runtime-hook",
        str(hook_path),
        "--hidden-import",
        "gi",
        "--hidden-import",
        "gi.repository.Gtk",
        "--hidden-import",
        "gi.repository.Gdk",
        "--hidden-import",
        "gi.repository.GLib",
        "--hidden-import",
        "gi.repository.Gio",
        "--hidden-import",
        "gi.repository.GdkPixbuf",
        "--hidden-import",
        "gi.repository.Pango",
        "--hidden-import",
        "gi.repository.cairo",
        "--hidden-import",
        "vlc",
        "--hidden-import",
        "setproctitle",
        "--hidden-import",
        "requests",
        "--hidden-import",
        "yt_dlp",
        "--hidden-import",
        "PIL",
        "--hidden-import",
        "PIL.Image",
        "--hidden-import",
        "PIL.ImageFilter",
        "--hidden-import",
        "multiprocessing.managers",
        "--collect-all",
        "gi",
        "--collect-all",
        "yt_dlp",
        "--collect-submodules",
        "gi.overrides",
        "--add-data",
        f"{PROJECT_ROOT / 'src'}{os.pathsep}src",
        "--add-data",
        f"{PROJECT_ROOT / 'src' / 'assets'}{os.pathsep}assets",
        str(PROJECT_ROOT / "src" / "__main__.py"),
    ]
    if gresource_path is not None:
        pyinstaller_cmd.extend([
            "--add-data",
            f"{gresource_path}{os.pathsep}.",
        ])
    if icon_path is not None and icon_path.exists():
        pyinstaller_cmd[pyinstaller_cmd.index("--windowed") + 1:pyinstaller_cmd.index("--windowed") + 1] = [
            "--icon",
            str(icon_path),
        ]

    env = os.environ.copy()
    env["WALLBLAZER_LOCAL_IPC"] = "1"
    env["PYGI_DLL_PATH"] = str(gtk_root / "bin")
    env["GI_TYPELIB_PATH"] = str(gtk_root / "lib" / "girepository-1.0")
    env["PATH"] = os.pathsep.join([str(gtk_root / "bin"), env.get("PATH", "")])

    print(f"[INFO] Using GTK runtime: {gtk_root}")
    if vlc_dir is not None:
        print(f"[INFO] Using VLC runtime: {vlc_dir}")

    subprocess.check_call(pyinstaller_cmd, cwd=PROJECT_ROOT, env=env)
    copy_gtk_runtime(gtk_root)
    copy_vlc_runtime(vlc_dir)
    if gresource_path is not None:
        DIST_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gresource_path, DIST_DIR / "wallblazer.gresource")

    exe_path = DIST_DIR / "Wall-Blazer.exe"
    print()
    print("Build complete.")
    print(f"Executable: {exe_path}")


if __name__ == "__main__":
    build()
