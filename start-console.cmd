@echo off
rem One-click launcher for the voice-ai console.
rem Keep this file ASCII-only: cmd.exe mangles UTF-8 batch files.
setlocal
set "PORT=8080"
set "HERE=%~dp0"
rem 2026-10-30: the local Qwen models were deleted, so nothing starts Ollama here
rem any more. To bring the local brain back: run `ollama serve`, pull a model, and
rem re-add a [brain.profiles.local] entry in ~/.voice-ai/brains.toml.

:bridge
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto checkmem

rem A second double-click while models are still loading would load them twice:
rem SO_REUSEADDR lets both processes bind 8080. Wait for the running one instead.
for /f %%n in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'bridge.server.py' }).Count"') do set "LOADING=%%n"
if "%LOADING%"=="0" goto launch
if "%LOADING%"=="" goto launch
echo Bridge is already loading; waiting for port %PORT% ...
goto wait

:checkmem
rem A bridge started without start-bridge.cmd runs with memory disabled; opening the
rem page then looks like "the shortcut did nothing". 1=on, 0=off, 2=cannot tell.
for /f %%m in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%bridge\check-memory.ps1"') do set "MEM=%%m"
if "%MEM%"=="0" goto memoff
goto open

:memoff
echo The running bridge was started without memory; restarting it (models load in 3-5 min).
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%bridge\shutdown-bridge.ps1"
if errorlevel 1 (
  echo Could not stop the running bridge. Close it, then start this again.
  pause
  exit /b 1
)

:launch
rem A killed bridge leaves the Feiyu worker inside WSL holding ~3.5 GB of VRAM.
rem No bridge is listening, so that worker is orphaned and safe to stop.
wsl.exe -d Ubuntu-24.04 -u root --exec pkill -f worker_feiyu.py >nul 2>&1

start "voice-ai bridge" cmd /c call "%HERE%start-bridge.cmd"

set /a tries=0
:wait
rem timeout.exe refuses to run when stdin is redirected; ping always waits.
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto open
set /a tries+=1
if %tries% lss 60 goto wait
echo.
echo The bridge did not start within 60 s. Scroll up in the bridge window for the error.
pause
exit /b 1

:open
start "" "http://127.0.0.1:%PORT%/console"
