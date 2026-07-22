@echo off
REM ---------------------------------------------------------------
REM  cue_pipeline - install as a Windows service via NSSM
REM  Run from an ELEVATED cmd / PowerShell (Run as administrator).
REM
REM  NSSM must be on PATH. Download from https://nssm.cc/download and
REM  drop nssm.exe somewhere on PATH (e.g. C:\Windows\System32\).
REM ---------------------------------------------------------------

setlocal
pushd "%~dp0"

set SERVICE=cue_pipeline
set PROJ=%CD%
set PY=%PROJ%\.venv\Scripts\python.exe
set SCRIPT=%PROJ%\main.py
set CONFIG=%PROJ%\config.yaml

where nssm >nul 2>&1
if errorlevel 1 (
    echo [X] nssm.exe is not on PATH. Install from https://nssm.cc/download
    popd
    exit /b 1
)

if not exist "%PY%" (
    echo [X] Virtualenv missing: %PY%
    echo     Run install.bat first.
    popd
    exit /b 1
)

echo Installing Windows service "%SERVICE%" ...
nssm install %SERVICE% "%PY%" "%SCRIPT%" --config "%CONFIG%"
if errorlevel 1 goto :fail

nssm set %SERVICE% AppDirectory "%PROJ%"
nssm set %SERVICE% Start SERVICE_AUTO_START
nssm set %SERVICE% AppStdout "%PROJ%\service.out.log"
nssm set %SERVICE% AppStderr "%PROJ%\service.err.log"
nssm set %SERVICE% AppRotateFiles 1
nssm set %SERVICE% AppRotateBytes 5242880
nssm set %SERVICE% DisplayName "CUE Pipeline (Lidarr helper)"
nssm set %SERVICE% Description "Watches downloads, splits CUE disc images with ffmpeg, hands off to Lidarr. Uses Ollama (qwen2.5:32b) for CUE repair + tag normalization."

echo Starting service ...
nssm start %SERVICE%
if errorlevel 1 goto :fail

echo.
echo [OK] Service "%SERVICE%" installed and started.
echo     Logs: %PROJ%\service.out.log  (and pipeline.log for the app log)
echo     Remove with:  uninstall_service.bat
popd
exit /b 0

:fail
echo.
echo [X] Service install failed. See output above.
popd
exit /b 1
