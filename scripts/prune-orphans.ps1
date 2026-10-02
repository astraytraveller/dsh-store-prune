# prune-orphans.ps1 -- thin entry point around prune-orphans.py
#
#   .\prune-orphans.ps1                 # dry run: reports what it would delete
#   .\prune-orphans.ps1 -Execute        # actually delete the orphaned store content
#   .\prune-orphans.ps1 -Execute -Quiet # for automation: no console output, log only
#
# Exits with the engine's status: 0 ok, 3 refused by a safety rail, 1 error.

[CmdletBinding()]
param(
  [switch]$Execute,
  [string]$Store,
  [string]$LogDir = (Join-Path $PSScriptRoot 'logs'),
  [switch]$Quiet,
  [switch]$Json,
  [string[]]$ExtraArgs = @()   # e.g. -ExtraArgs '--project','D:\x','--min-protected','1'
)

$ErrorActionPreference = 'Stop'

function Resolve-Python {
  if ($env:DSH_PRUNE_PYTHON -and (Test-Path $env:DSH_PRUNE_PYTHON)) { return $env:DSH_PRUNE_PYTHON }
  foreach ($candidate in @('C:\Python314\python.exe', "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe")) {
    if (Test-Path $candidate) { return $candidate }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { return $py.Source }
  throw 'python not found; set $env:DSH_PRUNE_PYTHON to the interpreter path'
}

$engine = Join-Path $PSScriptRoot 'prune-orphans.py'
if (-not (Test-Path $engine)) { throw "missing $engine" }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force $LogDir | Out-Null }
$log = Join-Path $LogDir ("prune-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))

$pyArgs = @($engine)
if ($Execute) { $pyArgs += '--execute' }
if ($Store)   { $pyArgs += @('--store', $Store) }
if ($Quiet)   { $pyArgs += '--quiet' }
if ($Json)    { $pyArgs += '--json' }
if ($ExtraArgs.Count) { $pyArgs += $ExtraArgs }
$pyArgs += @('--log', $log)

$env:PYTHONIOENCODING = 'utf-8'
$interpreter = Resolve-Python

if ($Quiet) {
  & $interpreter @pyArgs *> (Join-Path $LogDir 'last-run.out')
} else {
  & $interpreter @pyArgs
}
exit $LASTEXITCODE
