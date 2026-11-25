@echo off
cd /d "%~dp0"
call "%~dp0SETTINGS.bat"
if not exist ".venv\Scripts\python.exe" (echo Run SETUP.bat first. & pause & exit /b 1)
set "TESTOUT=%OUT%_test"
if exist "%TESTOUT%" rmdir /s /q "%TESTOUT%"
.venv\Scripts\python.exe code_replay.py "%SOURCE%" --out "%TESTOUT%" --plan test_plan.txt --only-plan --wpm 60 --think 0.5 --lookup-min 0.5 --lookup-max 1 --start-delay 10
pause
