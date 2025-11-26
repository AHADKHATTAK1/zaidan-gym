@echo off
setlocal
cd /d "%~dp0"

:: Ensure Python via py launcher or python
where py >nul 2>nul
if %ERRORLEVEL% neq 0 (
  where python >nul 2>nul
  if %ERRORLEVEL% neq 0 (
    echo Python not found. Please install Python 3.10+ and re-run.
    exit /b 1
  )
)

:: Create venv if missing
if not exist .venv (
  echo Creating virtual environment (.venv)...
  py -m venv .venv 2>nul || python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist .env (
  echo FLASK_SECURE_COOKIES=0>.env
  echo FLASK_DEBUG=1>>.env
  echo ADMIN_USERNAME=admin>>.env
  echo ADMIN_PASSWORD=admin123>>.env
  echo AUTO_BACKUP_ON_LOGIN=1>>.env
  echo AUTO_BACKUP_DEST=local>>.env
  echo Created .env with default local settings.
)

echo Starting server at http://127.0.0.1:5000
python app.py
