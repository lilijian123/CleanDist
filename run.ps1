param([switch]$Demo)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $systemPython) { throw 'Python 3.11 or later is required.' }
    & $systemPython.Source -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
& $projectPython -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in ('psutil','PySide6','send2trash')) else 1)"
if ($LASTEXITCODE -ne 0) {
    & $projectPython -m pip install --only-binary=:all: -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install dependencies. Check network access.' }
}
$appArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'main.py'))
if ($Demo) { $appArguments += '--demo' }
& $projectPython @appArguments
if ($LASTEXITCODE -ne 0) { throw 'The application exited with an error.' }
