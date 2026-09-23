@echo off
setlocal
cd /d "%~dp0..\.."
pythonw -m agent_trace_kit.desk --app
if errorlevel 1 python -m agent_trace_kit.desk
exit /b %ERRORLEVEL%
