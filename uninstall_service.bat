@echo off
REM Remove the cue_pipeline Windows service. Run from an ELEVATED prompt.

setlocal
set SERVICE=cue_pipeline

where nssm >nul 2>&1
if errorlevel 1 (
    echo [X] nssm.exe is not on PATH.
    exit /b 1
)

nssm stop %SERVICE% 2>nul
nssm remove %SERVICE% confirm
echo Service "%SERVICE%" removed.
exit /b 0
