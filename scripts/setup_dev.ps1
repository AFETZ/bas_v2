$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv from https://docs.astral.sh/uv/getting-started/installation/ and rerun.'
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    uv venv --python 3.10 .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 environment creation failed.' }
}
& .\.venv\Scripts\python.exe -c 'import sys; assert sys.version_info[:2] == (3, 10), "Python 3.10 required"'
if ($LASTEXITCODE -ne 0) { throw 'Existing .venv must use Python 3.10.' }
uv pip sync --python .venv\Scripts\python.exe .devcontainer\requirements-dev.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
uv pip check --python .venv\Scripts\python.exe
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed.' }
Write-Host 'Ready: .\.venv\Scripts\python.exe -m pytest <focused test file>'
Write-Host 'Linux tools and runtime: open this checkout in Ubuntu WSL, then bash scripts/setup_dev.sh'
