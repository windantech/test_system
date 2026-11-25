@echo off
cd /d "%~dp0"
call "%~dp0SETTINGS.bat"
if not exist ".venv\Scripts\python.exe" (echo Run SETUP.bat first. & pause & exit /b 1)
.venv\Scripts\python.exe code_replay.py "%SOURCE%" --out "%OUT%" --plan build_plan.txt --max-hours %HOURS% %EXCLUDES%
pause
