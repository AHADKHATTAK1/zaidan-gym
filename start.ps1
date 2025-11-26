Param(
  [int]$Port = 5000
)

Write-Host "[+] Starting Gym app on port $Port..." -ForegroundColor Cyan

$Env:FLASK_SECURE_COOKIES = '0'
$Env:FLASK_DEBUG = '1'
$Env:PORT = "$Port"

# Best-effort install (Windows will ignore gunicorn/gevent due to markers)
try {
  Write-Host "[+] Installing dependencies (pip)" -ForegroundColor DarkCyan
  py -m pip install -r "requirements.txt"
} catch {
  Write-Warning "pip install failed; continuing to start the app"
}

# Run the app
try {
  Write-Host "[+] Launching app (py app.py)" -ForegroundColor DarkCyan
  py app.py
} catch {
  Write-Error "App failed to start. $_"
  exit 1
}