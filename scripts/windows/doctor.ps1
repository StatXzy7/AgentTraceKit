$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
python -m agent_trace_kit.cli doctor
exit $LASTEXITCODE
