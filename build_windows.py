import os
import subprocess
import sys

def build():
    print("Building Wall Blazer for Windows...")
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", "Wall-Blazer",
        "--windowed",
        "--hidden-import", "gi",
        "--hidden-import", "vlc",
        "--add-data", f"src/assets{os.pathsep}assets",
        "src/__main__.py"
    ])
    print("Build complete. Please copy your libvlc libraries into the dist folder.")

if __name__ == "__main__":
    build()
