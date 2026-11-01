# memory-ledger installer for Windows: make sure a Python this project supports
# is there (installing one under ~\.second_brain when not), then run
# scripts\setup.py, which does the rest and puts a launcher on the Desktop.
#   powershell -ExecutionPolicy Bypass -File install.ps1 [setup.py options]
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$want = (Get-Content (Join-Path $here '.python-version') -Raw).Trim()
$minMajor = 3; $minMinor = 11
$home_ = if ($env:SECOND_BRAIN_HOME) { $env:SECOND_BRAIN_HOME } else { Join-Path $env:USERPROFILE '.second_brain' }

function Test-Python($exe, $pre) {
  try { & $exe @pre -c "import sys; sys.exit(sys.version_info < ($minMajor, $minMinor))" 2>$null; return $LASTEXITCODE -eq 0 }
  catch { return $false }
}
$setup = Join-Path $here 'scripts\setup.py'
foreach ($c in @(@('py', @('-3')), @('python', @()), @('python3', @()))) {
  if ((Get-Command $c[0] -ErrorAction SilentlyContinue) -and (Test-Python $c[0] $c[1])) {
    & $c[0] @($c[1]) $setup --launcher @args
    exit $LASTEXITCODE
  }
}

Write-Host "No Python $minMajor.$minMinor+ found; installing Python $want under $home_\python (uv, nothing system-wide)."
New-Item -ItemType Directory -Force -Path $home_ | Out-Null
$env:UV_PYTHON_INSTALL_DIR = Join-Path $home_ 'python'
$env:UV_UNMANAGED_INSTALL = Join-Path $home_ 'bin'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  if (-not (Test-Path (Join-Path $env:UV_UNMANAGED_INSTALL 'uv.exe'))) {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  }
  $env:Path = "$($env:UV_UNMANAGED_INSTALL);$env:Path"
}
uv python install $want
$py = (uv python find $want).Trim()
& $py $setup --launcher @args
exit $LASTEXITCODE
