@echo off
rem Quick check of the live preview (about 5-10 minutes). The 44 sign-in files are
rem written instantly instead of typed; from there it runs exactly like the real
rem recording: .env, database, migrations, server, then Chrome: login, set password, sign in.
rem It uses its own folder, database container and ports, so it never touches a recording.
cd /d "%~dp0"
call "%~dp0SETTINGS.bat"
if not exist ".venv\Scripts\python.exe" (echo Run SETUP.bat first. & pause & exit /b 1)
set "TESTOUT=%OUT%_preview_test"
if exist "%TESTOUT%" rmdir /s /q "%TESTOUT%"
docker rm -f fpc-test-db >nul 2>&1
.venv\Scripts\python.exe code_replay.py "%SOURCE%" --out "%TESTOUT%" --plan preview_test_plan.txt --only-plan %EXCLUDES% --skip-to "Development environment" --replace fpc-dev-db=fpc-test-db --replace 3307=3308 --replace 127.0.0.1:8080=127.0.0.1:8081 --app-url http://127.0.0.1:8081 --start-delay 10
echo.
echo Cleaning up the test database container...
docker rm -f fpc-test-db >nul 2>&1
echo Close the VS Code window of the test folder to stop its web server.
pause

