@echo off
rem ===== Edit these two lines for this computer =====
rem SOURCE = the folder with the existing system's code
rem OUT    = an EMPTY folder where it will be rebuilt on camera
set "SOURCE=%USERPROFILE%\Documents\folder"
set "OUT=%USERPROFILE%\Documents\folder_rebuilt"

rem Build plan:  video_plan.txt         = setup pre-loaded; sign-in first, then each feature,
rem                                       built, run in Chrome and tested on screen
rem              login_preview_plan.txt = sign-in first, everything typed
rem              preview_plan.txt       = original order, with the app in Chrome
rem              build_plan.txt         = original order, code and tests only
set "PLAN=video_plan.txt"

rem Hours per recording session
set "HOURS=3"

rem Data files that are not typed (they are not hand-written code)
set EXCLUDES=--exclude "data-import/*" --exclude "*.csv" --exclude "*.jsonl" --exclude "public/demo/*" --exclude "public/demo.html" --exclude "public/guide.html" --exclude "tests/fixtures/*"

