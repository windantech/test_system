@echo off
rem ===== Edit these two lines for this computer =====
rem SOURCE = the folder with the existing system's code
rem OUT    = an EMPTY folder where it will be rebuilt on camera
set "SOURCE=%USERPROFILE%\Documents\folder"
set "OUT=%USERPROFILE%\Documents\folder_rebuilt"

rem Build plan:  preview_plan.txt = also runs the app and uses it in Chrome as it is built
rem              build_plan.txt   = code and tests only
set "PLAN=preview_plan.txt"

rem Hours per recording session
set "HOURS=3"

rem Data files that are not typed (they are not hand-written code)
set EXCLUDES=--exclude "data-import/*" --exclude "*.csv" --exclude "*.jsonl" --exclude "public/demo/*" --exclude "public/demo.html" --exclude "public/guide.html" --exclude "tests/fixtures/*"
