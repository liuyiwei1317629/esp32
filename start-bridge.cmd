@echo off
set MEMORY_ENABLED=1
set MEMORY_DRY_RUN=0
cd /d C:\Users\DELL\voice-ai
if "%~1"=="" (
  ".venv\Scripts\python.exe" bridge\server.py --serve
) else (
  ".venv\Scripts\python.exe" bridge\server.py %*
)
