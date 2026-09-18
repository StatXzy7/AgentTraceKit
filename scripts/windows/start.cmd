@echo off
setlocal
set "ROOT=%~dp0..\.."
python -m agent_trace_kit.cli %*
exit /b %ERRORLEVEL%
