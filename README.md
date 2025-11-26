# Combined Gym Fee System (Flask + SQLite + Tkinter + CLI)

Features:

- Flask web app with simple frontend (templates/index.html)
- SQLite DB (gym.db) with SQLAlchemy in `app.py`
- Tkinter desktop app `desktop_app.py` that uses the same `gym.db`
- CLI tool `cli.py` to add members or export payments
- Excel export via pandas/openpyxl

Client Quick Start (Windows):

1. Double-click `Start-Server.bat` (or run `Start-Server.ps1`).
   - It creates `.venv`, installs dependencies, generates `.env` with safe defaults, and starts the app.
2. Open http://127.0.0.1:5000 in your browser.
3. Login with `admin` / `admin123` (change later in settings).

Developer Quick start:

1. Create a virtualenv and install requirements:
   ```
   python -m venv venv
   source venv/bin/activate   # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```
2. Run the Flask app:
   ```
   python app.py
   ```
   Open http://127.0.0.1:5000
3. Or run desktop UI:
   ```
   python desktop_app.py
   ```
4. CLI usage:
   ```
   python cli.py add --name "Zaidan" --phone "0300" --admission 2024-03-15
   python cli.py export --id 1 --out member_1.xlsx
   ```

## Auth & Roles

- Login endpoints are enabled. A default admin is auto-seeded on first run using environment variables `ADMIN_USERNAME` and `ADMIN_PASSWORD` (defaults: `admin` / `admin123`).
- Users have roles: `admin` and `staff`. Admin-only endpoints are under `/admin/*` and require elevated access (e.g., settings, backups, audit verify, CSV import, WhatsApp test/status, schedule run-now).

## Environment Variables (optional but recommended)

- `SECRET_KEY`: Flask session secret.
- `DATABASE_URL`: e.g., `postgresql://user:pass@host/dbname` (falls back to local SQLite `gym.db`).
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`: Enable Google Sign-In.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS`: Email sending.
- `BACKUP_TO_EMAIL`: Recipient for email backups.
- `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_DEFAULT_COUNTRY_CODE`: WhatsApp Cloud API.
- `WHATSAPP_TEMPLATE_FEE_REMINDER_NAME`, `WHATSAPP_TEMPLATE_LANG`: Optional template-based reminders.
- `SCHEDULE_REMINDERS_ENABLED` (`1`/`0`), `SCHEDULE_TIME_HH`, `SCHEDULE_TIME_MM`: Daily reminder scheduler.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`: Seed first admin user on first run.

## Production Notes

- Gunicorn entry: `web: gunicorn -w 2 -k gevent app:app` (Procfile included).
- Configure `HOST`/`PORT`/`FLASK_DEBUG` as needed. For HTTPS-only cookies locally, set `FLASK_SECURE_COOKIES=0` for http.
- DB migrations are wired via Flask-Migrate; for long-lived installs consider running migrations explicitly.

## Auto Messenger (Twilio + Gmail)

Quick setup:

- Ensure `.env` contains:
  - `TWILIO_SID`, `TWILIO_AUTH`, `TWILIO_WHATSAPP` (Twilio sandbox: `whatsapp:+14155238886`)
  - `GMAIL_EMAIL`, `GMAIL_PASSWORD` (Google App Password, not normal password)

Run the mini service:

```
python auto_messenger.py
```

Send a test request:

```
curl -X POST http://127.0.0.1:5000/send \
   -H "Content-Type: application/json" \
   -d "{\"name\":\"Ahad\", \"whatsapp\":\"+923179880100\", \"email\":\"zaidanfitnessgym@gmail.com\"}"
```

Or call directly in Python REPL:

```
from auto_messenger import send_whatsapp, send_email
send_whatsapp("+923179880100", "Hello! This is a test message.")
send_email("zaidanfitnessgym@gmail.com", "Test", "Message sent!")
```
