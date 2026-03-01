@echo off
setlocal enabledelayedexpansion
title Wall Blazer - Windows Automated Installer

echo ===========================================
echo       Wall Blazer Installer for Windows    
echo ===========================================
echo.

:: 1. Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Python is not installed. Downloading Python 3.11...
    curl -L -o python_installer.exe https://www.python.org/ftp/python/3.11.5/python-3.11.5-amd64.exe
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to download Python. Please install it manually from python.org
        pause
        exit /b 1
    )
    echo [INFO] Installing Python silently...
    start /wait python_installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
    del python_installer.exe
    
    :: Refresh path for the current session
    set PATH=%PATH%;C:\Program Files\Python311\;C:\Program Files\Python311\Scripts\
) else (
    echo [INFO] Python is already installed.
)

:: 2. Check for VLC
if exist "C:\Program Files\VideoLAN\VLC\vlc.exe" (
    echo [INFO] VLC Media Player is already installed.
) else if exist "C:\Program Files (x86)\VideoLAN\VLC\vlc.exe" (
    echo [INFO] VLC Media Player (32-bit) is already installed.
) else (
    echo [INFO] VLC Media Player is not installed. Downloading VLC...
    curl -L -o vlc_installer.exe https://get.videolan.org/vlc/3.0.18/win64/vlc-3.0.18-win64.exe
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to download VLC. Please install it manually from videolan.org
        pause
        exit /b 1
    )
    echo [INFO] Installing VLC silently...
    start /wait vlc_installer.exe /L=1033 /S
    del vlc_installer.exe
)

:: 3. Create virtual environment and install requirements
echo.
echo [INFO] Setting up Python virtual environment...
if not exist "venv" (
    python -m venv venv
)
call venv\Scripts\activate.bat

echo [INFO] Installing Python dependencies...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
pip install pyinstaller

:: 4. Build the executable
echo.
echo [INFO] Compiling Wall Blazer into an executable...
python build_windows.py
if !errorlevel! neq 0 (
    echo [ERROR] Build failed. Check the logs above.
    pause
    exit /b 1
)

:: 5. Copy to Startup folder
echo.
echo [INFO] Adding Wall Blazer to Windows Startup...
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
copy /Y "dist\Wall-Blazer\Wall-Blazer.exe" "%STARTUP_DIR%\Wall-Blazer.exe"

echo.
echo ===========================================
echo   Installation Complete!                   
echo   Wall Blazer has been built and added     
echo   to your startup folder.                  
echo.
echo   You can find the standalone exe at:      
echo   %CD%\dist\Wall-Blazer\Wall-Blazer.exe
echo ===========================================
pause
exit /b 0
