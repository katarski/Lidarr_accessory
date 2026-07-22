@echo off
REM ---------------------------------------------------------------
REM  cue_pipeline - interactive launcher
REM  Double-click this on the RTX box to start watching the folder.
REM  Ctrl+C to stop.
REM ---------------------------------------------------------------

setlocal
pushd "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [X] .venv is missing. Run install.bat first.
    pause
    popd
    exit /b 1
)

if not exist "config.yaml" (
    echo [X] config.yaml is missing.
    pause
    popd
    exit /b 1
)

echo Starting cue_pipeline. Press Ctrl+C to stop.
echo.

call ".venv\Scripts\activate.bat"
python main.py --config config.yaml
set EXITCODE=%errorlevel%

echo.
if not "%EXITCODE%"=="0" (
    echo cue_pipeline exited with code %EXITCODE%.
    pause
)

popd
exit /b %EXITCODE%
