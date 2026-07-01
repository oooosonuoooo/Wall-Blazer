@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Wall Blazer - Windows Installer
color 0A

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "MSYS_ROOT=C:\msys64"
set "MSYS_BASH=%MSYS_ROOT%\usr\bin\bash.exe"
set "MSYS_ENV=%MSYS_ROOT%\usr\bin\env.exe"
set "MSYS_INSTALLER=%TEMP%\msys2-x86_64-latest.exe"
set "PYTHON_EXE=%MSYS_ROOT%\ucrt64\bin\python.exe"
set "DIST_DIR=%ROOT%dist\Wall-Blazer"
set "EXE=%DIST_DIR%\Wall-Blazer.exe"

echo ===========================================
echo        Wall Blazer Windows Installer
echo ===========================================
echo.

if not exist "%MSYS_BASH%" (
    echo [INFO] MSYS2 UCRT64 was not found. Downloading the latest MSYS2 installer...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/msys2/msys2-installer/releases/latest/download/msys2-x86_64-latest.exe' -OutFile '%MSYS_INSTALLER%'"
    if errorlevel 1 (
        echo [ERROR] Failed to download MSYS2.
        pause
        exit /b 1
    )

    echo [INFO] Installing MSYS2 to %MSYS_ROOT%...
    start /wait "" "%MSYS_INSTALLER%" in --confirm-command --accept-messages --root C:/msys64
    if errorlevel 1 (
        echo [ERROR] MSYS2 installation failed.
        pause
        exit /b 1
    )
)

call :run_ucrt "pacman -Syuu --noconfirm"
call :run_ucrt "pacman -Syuu --noconfirm"
call :run_ucrt "pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-pip mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-python-cairo mingw-w64-ucrt-x86_64-gtk3 mingw-w64-ucrt-x86_64-gobject-introspection mingw-w64-ucrt-x86_64-python-pillow mingw-w64-ucrt-x86_64-python-requests mingw-w64-ucrt-x86_64-python-setproctitle mingw-w64-ucrt-x86_64-python-yt-dlp"
if errorlevel 1 (
    echo [ERROR] Failed to install MSYS2 runtime packages.
    pause
    exit /b 1
)

call :run_ucrt "pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-webkitgtk-4.1" >nul 2>nul

if not exist "%PYTHON_EXE%" (
    echo [ERROR] UCRT64 Python was not installed correctly.
    pause
    exit /b 1
)

echo [INFO] Checking VLC installation...
set "VLC_DIR="
if exist "C:\Program Files\VideoLAN\VLC\vlc.exe" set "VLC_DIR=C:\Program Files\VideoLAN\VLC"
if exist "C:\Program Files (x86)\VideoLAN\VLC\vlc.exe" set "VLC_DIR=C:\Program Files (x86)\VideoLAN\VLC"
if not defined VLC_DIR (
    echo [INFO] VLC not found. Downloading VLC 3.0.21...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Invoke-WebRequest -UseBasicParsing -Uri 'https://get.videolan.org/vlc/3.0.21/win64/vlc-3.0.21-win64.exe' -OutFile '%TEMP%\wallblazer-vlc.exe'"
    if errorlevel 1 (
        echo [ERROR] Failed to download VLC.
        pause
        exit /b 1
    )
    start /wait "" "%TEMP%\wallblazer-vlc.exe" /L=1033 /S
    del /f /q "%TEMP%\wallblazer-vlc.exe" >nul 2>nul
    if exist "C:\Program Files\VideoLAN\VLC\vlc.exe" set "VLC_DIR=C:\Program Files\VideoLAN\VLC"
)

set "PATH=%MSYS_ROOT%\ucrt64\bin;%MSYS_ROOT%\usr\bin;%PATH%"
set "MSYSTEM_PREFIX=%MSYS_ROOT%\ucrt64"
set "GTK_RUNTIME_ROOT=%MSYS_ROOT%\ucrt64"
set "WALLBLAZER_LOCAL_IPC=1"
if defined VLC_DIR set "VLC_DIR=%VLC_DIR%"

echo [INFO] Installing Python build dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)
"%PYTHON_EXE%" -m pip install -r requirements.txt pyinstaller python-vlc
if errorlevel 1 (
    echo [ERROR] Failed to install Python dependencies.
    pause
    exit /b 1
)

echo [INFO] Building Wall Blazer...
"%PYTHON_EXE%" build_windows.py
if errorlevel 1 (
    echo [ERROR] Windows build failed.
    pause
    exit /b 1
)

if not exist "%EXE%" (
    echo [ERROR] Build finished but %EXE% was not created.
    pause
    exit /b 1
)

echo [INFO] Creating Desktop, Start Menu, and Startup shortcuts...
set "DESKTOP_SHORTCUT=%USERPROFILE%\Desktop\Wall Blazer.lnk"
set "START_MENU_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Wall Blazer"
set "START_MENU_SHORTCUT=%START_MENU_DIR%\Wall Blazer.lnk"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "STARTUP_SHORTCUT=%STARTUP_DIR%\Wall Blazer.lnk"

if not exist "%START_MENU_DIR%" mkdir "%START_MENU_DIR%" >nul 2>nul
del /f /q "%STARTUP_DIR%\Wall-Blazer.exe" >nul 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$shell = New-Object -ComObject WScript.Shell;" ^
  "$target = '%EXE%';" ^
  "$workDir = '%DIST_DIR%';" ^
  "$entries = @(@{Path='%DESKTOP_SHORTCUT%'; Args=''}, @{Path='%START_MENU_SHORTCUT%'; Args=''}, @{Path='%STARTUP_SHORTCUT%'; Args='-b'});" ^
  "foreach ($entry in $entries) { $s = $shell.CreateShortcut($entry.Path); $s.TargetPath = $target; $s.Arguments = $entry.Args; $s.WorkingDirectory = $workDir; $s.Description = 'Wall Blazer - Video Wallpaper'; $s.IconLocation = $target + ',0'; $s.Save() }"
if errorlevel 1 (
    echo [ERROR] Failed to create shortcuts.
    pause
    exit /b 1
)

echo.
echo ===========================================
echo Installation complete.
echo Executable: %EXE%
echo ===========================================
echo.
pause
exit /b 0

:run_ucrt
"%MSYS_ENV%" MSYSTEM=UCRT64 CHERE_INVOKING=1 MSYS2_PATH_TYPE=inherit "%MSYS_BASH%" -lc "%~1"
exit /b %ERRORLEVEL%
