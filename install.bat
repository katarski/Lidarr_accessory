@echo off
REM ---------------------------------------------------------------
REM  cue_pipeline - first-time setup for the RTX 3090 box
REM  Run this ONCE from an elevated PowerShell / cmd if possible.
REM ---------------------------------------------------------------

setlocal EnableDelayedExpansion
pushd "%~dp0"

echo.
echo ==============================================================
echo  cue_pipeline installer
echo  Folder: %CD%
echo ==============================================================
echo.

REM --- 1. Python check --------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [X] Python is not on PATH.
    echo     Install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo     and make sure "Add python.exe to PATH" is ticked.
    goto :fail
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% found.

REM --- 2. ffmpeg / ffprobe check ---------------------------------------
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [X] ffmpeg is not on PATH.
    echo     Grab a static Windows build from https://www.gyan.dev/ffmpeg/builds/
    echo     Unzip to e.g. C:\ffmpeg, then add C:\ffmpeg\bin to your system PATH.
    goto :fail
)
echo [OK] ffmpeg found.

where ffprobe >nul 2>&1
if errorlevel 1 (
    echo [X] ffprobe is not on PATH. It ships with ffmpeg; make sure
    echo     you installed the *full* build, not just ffmpeg.exe.
    goto :fail
)
echo [OK] ffprobe found.

REM --- 3. Virtual environment ------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [..] Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 goto :fail
) else (
    echo [OK] .venv already exists.
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 goto :fail

echo [..] Upgrading pip ...
python -m pip install --upgrade pip >nul
if errorlevel 1 goto :fail

echo [..] Installing Python dependencies ...
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo [OK] Dependencies installed.

REM --- 4. Ollama check + model pull ------------------------------------
REM  Using ollama.exe (not "ollama") so Windows does not mistake the
REM  project's ollama_client.py for the CLI when resolving PATHEXT.
where ollama.exe >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!] Ollama is not on PATH on this machine.
    echo     If Ollama runs here, install it from https://ollama.com
    echo     If it runs on a different machine, edit config.yaml
    echo     ^(ollama.base_url^) and skip the model pull step below.
    echo.
) else (
    echo [OK] Ollama found -- pulling qwen2.5:32b ^(~19 GB^).
    echo     This takes a while on first run. Subsequent runs are instant.
    ollama.exe pull qwen2.5:32b
    if errorlevel 1 (
        echo [!] ollama pull failed. You can retry manually later.
    ) else (
        echo [OK] Model ready.
    )
)

REM --- 5. Config reminder ----------------------------------------------
echo.
echo ==============================================================
echo  Install complete. Before first run:
echo.
echo    1. Open config.yaml and set:
echo         - lidarr.api_key
echo         - lidarr.base_url
echo         - lidarr.path_mapping.from / to
echo         - lidarr.library_root_windows
echo         - lidarr.library_root_lidarr
echo         - watch.root   ^(if different from V:/Dan/Internet Downloads^)
echo.
echo    2. Run the service:  run.bat
echo.
echo    3. Optional, install as a Windows service: install_service.bat
echo ==============================================================
goto :end

:fail
echo.
echo [X] Install failed. Fix the error above and re-run install.bat
popd
exit /b 1

:end
popd
exit /b 0
