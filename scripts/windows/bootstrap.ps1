$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
python -m pip install -e $Root
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
