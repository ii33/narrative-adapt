@echo off
setlocal
rem Thin forwarder kept for people and scripts used to running start.cmd.
rem start-desktop.cmd owns the whole chain: it finds the project Python, runs the
rem Python launcher and decides whether to pause on failure.
call "%~dp0start-desktop.cmd" %*
set "narrative_exit_code=%errorlevel%"
exit /b %narrative_exit_code%
