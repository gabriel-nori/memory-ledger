# memory-ledger setup on Windows: find a Python 3 and run setup.py.
# Every argument passes through:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 --voice
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$setup = Join-Path $PSScriptRoot "setup.py"
$candidates = @(@("py", "-3"), @("python"), @("python3"))
foreach ($c in $candidates) {
    $exe = Get-Command $c[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $pre = @($c | Select-Object -Skip 1)
    & $exe.Source @pre -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $exe.Source @pre $setup @args
        exit $LASTEXITCODE
    }
}
Write-Error "memory-ledger setup: no Python 3.11+ found. Install it from python.org or with 'winget install Python.Python.3.13', then run this again."
exit 1
