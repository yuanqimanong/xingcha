@echo off
rem ===========================================================================
rem xingcha . Windows: pull the latest code, then start. DOUBLE-CLICK THIS.
rem
rem   xc.bat        start without touching git (the reproducible one)
rem   this file     git pull --ff-only, then the same start
rem
rem Why two files instead of one that always pulls: `start` has to be the
rem reproducible one -- run THIS checkout, no network needed. A pull hidden
rem inside start turns "it worked yesterday, one double-click later it does
rem not" into a class of failure nobody can trace. deploy/linux/xc splits the
rem same way.
rem ===========================================================================
rem ---------------------------------------------------------------------------
rem ASCII-only ON PURPOSE -- same reason as xc.bat: cmd.exe under `chcp 65001`
rem re-seeks by BYTE offset while counting characters and can resume reading in
rem the MIDDLE of a multi-byte char, running the tail of the line AS A COMMAND.
rem All logic and all Chinese output live in xc.ps1.
rem ---------------------------------------------------------------------------

setlocal
set "PS1=%~dp0xc.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" update
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
