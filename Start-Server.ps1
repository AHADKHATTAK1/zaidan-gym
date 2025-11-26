# Simple one-click start for Windows PowerShell
$ErrorActionPreference = 'Stop'
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $HERE

# Ensure Python is available
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host 'Python not found on PATH. Please install Python 3.10+ from python.org and re-run.' -ForegroundColor Yellow
    exit 1
}

# Create venv if missing
if (-not (Test-Path .venv)) {
    Write-Host 'Creating virtual environment (.venv)...'
    python -m venv .venv
}

# Activate venv
. .\.venv\Scripts\Activate.ps1

# Install deps
python -m pip install --upgrade pip
pip install -r requirements.txt

# Create .env with sensible defaults if missing
if (-not (Test-Path .env)) {
    @"
FLASK_SECURE_COOKIES=0
FLASK_DEBUG=1
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
AUTO_BACKUP_ON_LOGIN=1
AUTO_BACKUP_DEST=local
"@ | Out-File -Encoding utf8 .env
    Write-Host 'Created .env with default local settings.' -ForegroundColor Green
}

# Run server
Write-Host 'Starting server at http://127.0.0.1:5000' -ForegroundColor Cyan
python app.py
