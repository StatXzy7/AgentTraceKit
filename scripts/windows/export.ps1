$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
python -m agent_trace_kit.cli pair export @args
exit $LASTEXITCODE
