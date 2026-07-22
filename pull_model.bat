@echo off
REM ---------------------------------------------------------------
REM  cue_pipeline - pull / refresh the Ollama model
REM
REM  Reads the model name from config.yaml (ollama.model) so you only
REM  have to change it in one place. Double-click to run.
REM
REM  Uses "ollama.exe" (not "ollama") so Windows never mistakes a
REM  neighbouring ollama.py for the real binary.
REM ---------------------------------------------------------------

setlocal EnableDelayedExpansion
pushd "%~dp0"

where ollama.exe >nul 2>&1
if errorlevel 1 (
    echo [X] Ollama is not on PATH on this machine.
    echo     If Ollama runs on a different host, run the pull there instead:
    echo        ollama.exe pull qwen2.5:32b
    pause
    popd
    exit /b 1
)

REM Extract the model name from config.yaml (strips quotes, ignores comments).
set MODEL=
for /f "usebackq tokens=1,* delims=:" %%a in ("config.yaml") do (
    set KEY=%%a
    set VAL=%%b
    if /i "!KEY: =!"=="model" if "!MODEL!"=="" (
        set RAW=!VAL!
        for /f "tokens=* delims= " %%x in ("!RAW!") do set RAW=%%x
        set RAW=!RAW:"=!
        for /f "tokens=1 delims=#" %%y in ("!RAW!") do set MODEL=%%y
        for /l %%z in (1,1,20) do if "!MODEL:~-1!"==" " set MODEL=!MODEL:~0,-1!
    )
)

if "%MODEL%"=="" (
    echo [X] Could not read ollama.model from config.yaml.
    pause
    popd
    exit /b 1
)

echo.
echo Pulling model: %MODEL%
echo This takes a while on first run (tens of GB for 32B models).
echo.
ollama.exe pull %MODEL%
set EXITCODE=%errorlevel%

echo.
if "%EXITCODE%"=="0" (
    echo [OK] Model %MODEL% is ready.
    echo      Installed models:
    ollama.exe list
) else (
    echo [X] ollama.exe pull failed with code %EXITCODE%.
)

popd
pause
exit /b %EXITCODE%
