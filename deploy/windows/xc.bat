@echo off
rem ===========================================================================
rem xingcha . Windows local run. DOUBLE-CLICK THIS FILE.
rem
rem   deploy\README.md        why no docker here, .env, the firewall step
rem   deploy\edge\edge.bat    the HTTPS gateway (a separate double-click)
rem ===========================================================================
rem ---------------------------------------------------------------------------
rem This file is ASCII-only ON PURPOSE. Do not put Chinese (or any multi-byte
rem text) back in here -- not even in a rem line.
rem
rem cmd.exe under `chcp 65001` re-seeks the .bat by BYTE offset while counting
rem characters, so it can resume reading in the MIDDLE of a multi-byte char.
rem The tail of the line then runs AS A COMMAND. It is intermittent: it bites
rem when the file is not in the OS page cache (right after an edit, or a cold
rem boot), and "works fine" the next time. A rem line documenting
rem `reset-password` really did execute once.
rem
rem PowerShell does not have that bug, so all logic and all Chinese output live
rem in %PS1%. tests/test_deploy_artifacts.py asserts this file stays ASCII.
rem ---------------------------------------------------------------------------

setlocal
set "PS1=%~dp0xc.ps1"

rem -ExecutionPolicy Bypass: the default policy blocks unsigned .ps1, and the
rem failure reads like a security lecture rather than "double-click me".
rem Scope is this one process only; nothing on the machine is changed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"

rem Pause only on failure, and only when double-clicked (cmd /c leaves the
rem window open anyway). Without it the window vanishes with the error in it.
if not "%RC%"=="0" pause
exit /b %RC%
