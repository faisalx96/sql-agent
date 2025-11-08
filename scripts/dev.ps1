Param()

$ErrorActionPreference = "Stop"

# Resolve project root (parent of scripts/)
$ROOT_DIR = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT_DIR

$VENV_DIR = Join-Path $ROOT_DIR ".venv"

if (-not (Test-Path $VENV_DIR)) {
  Write-Host "[dev] Creating virtualenv at .venv" -ForegroundColor Yellow
  $python3 = (Get-Command python3 -ErrorAction SilentlyContinue)
  $python  = (Get-Command python  -ErrorAction SilentlyContinue)
  $pyPath  = if ($python3) { $python3.Path } elseif ($python) { $python.Path } else { "python" }
  & $pyPath -m venv $VENV_DIR
}

# Activate venv
. (Join-Path $VENV_DIR "Scripts\Activate.ps1")

Write-Host "[dev] Using python: $((Get-Command python).Path)" -ForegroundColor Cyan

Write-Host "[dev] Ensuring dependencies (from requirements.txt)" -ForegroundColor Yellow
python -m pip install -U pip | Out-Null
python -m pip install -r (Join-Path $ROOT_DIR "requirements.txt") | Out-Null

# PYTHONPATH so 'app' is importable
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$ROOT_DIR;$env:PYTHONPATH" } else { "$ROOT_DIR" }

$hostAddr = if ($env:APP_HOST) { $env:APP_HOST } else { "127.0.0.1" }
$portNum  = if ($env:APP_PORT) { $env:APP_PORT } else { "8000" }

Write-Host "[dev] Starting server on ${hostAddr}:${portNum}" -ForegroundColor Green
python -m uvicorn app.server:app --reload --host $hostAddr --port $portNum


