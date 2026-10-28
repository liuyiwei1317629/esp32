@echo off
rem One-click launcher for the voice-ai console.
rem Keep this file ASCII-only: cmd.exe mangles UTF-8 batch files.
setlocal
set "PORT=8080"
set "HERE=%~dp0"
set "OLLAMA_EXE=D:\ollama\ollama.exe"

rem The local brain is optional; start Ollama only when it is not already up.
netstat -ano | findstr ":11434" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto bridge
if exist "%OLLAMA_EXE%" start "Ollama" /min "%OLLAMA_EXE%" serve

:bridge
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto open

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
