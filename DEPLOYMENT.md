# Deployment Guide

This app is a Flask + SQLAlchemy gym tracker with optional Google Sign-In, WhatsApp Cloud API reminders, email backups, and admin/staff roles.

## Prerequisites

- Python 3.10+
- A database (default SQLite; recommended: PostgreSQL via `DATABASE_URL`)
- Optional: WhatsApp Cloud API credentials
- Optional: SMTP credentials for email backups
- Optional: Google OAuth Client (ID/Secret) for Sign-In

## Environment Variables

Set as appropriate for your environment. For local development you can also use a `.env` file in the project root.

- `SECRET_KEY`: Flask session secret (use a strong random value)
- `DATABASE_URL`: e.g., `postgresql://user:pass@host:5432/dbname` (fallback is local `sqlite:///gym.db`)
- `HOST` / `PORT`: Flask dev server bind (defaults: `0.0.0.0:5000`)
- `FLASK_DEBUG`: `1` to enable debug locally
- `FLASK_SECURE_COOKIES`: set `0` for local http (cookies otherwise marked secure)
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`: first admin user on first run (defaults: `admin` / `admin123`)

Backups:

- `AUTO_BACKUP_ON_LOGIN`: set `1` to create a backup automatically after any user logs in
- `AUTO_BACKUP_DEST`: comma-separated destinations: `local`, `email`, `drive` (default: `local`)
- `BACKUP_TO_EMAIL`: recipient address for backup emails (used when `AUTO_BACKUP_DEST` contains `email`)
- `GOOGLE_SERVICE_ACCOUNT_FILE`, `DRIVE_FOLDER_ID`: required for Drive uploads (used when `AUTO_BACKUP_DEST` contains `drive`)

Auth (optional):

- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`: enable Google Sign-In (GIS + OAuth)

Email (optional):

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS`
- `BACKUP_TO_EMAIL`: recipient for `/admin/backup/email`

WhatsApp (optional):

- `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_DEFAULT_COUNTRY_CODE`: e.g., `92` (used to normalize numbers)
- `WHATSAPP_TEMPLATE_FEE_REMINDER_NAME`, `WHATSAPP_TEMPLATE_LANG`: template-based reminders

Scheduler (optional):

- `SCHEDULE_REMINDERS_ENABLED`: `1` to enable daily reminders
- `SCHEDULE_TIME_HH`, `SCHEDULE_TIME_MM`: 24h schedule time

## Local Development (Windows PowerShell)

```powershell
python -m venv venv
./venv/Scripts/Activate.ps1
pip install -r requirements.txt

# Minimal local env (http)
$env:FLASK_DEBUG = "1"
$env:FLASK_SECURE_COOKIES = "0"
$env:ADMIN_USERNAME = "admin"; $env:ADMIN_PASSWORD = "admin123"

python app.py
# Open http://127.0.0.1:5000
```

## Database Migrations (Flask-Migrate)

For long-lived installs, manage schema with migrations:

```powershell
# One-time init (creates migrations/)
$env:FLASK_APP = "app.py"
flask db init
# Generate migration from model changes
flask db migrate -m "initial"
# Apply to target DB
flask db upgrade
```

When using `DATABASE_URL`, ensure it points to your Postgres instance before `flask db upgrade`.

## Production (Linux)

- Use gunicorn with gevent (Procfile provided):

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="<strong-random>"
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
export HOST=0.0.0.0; export PORT=5000
# Optional: other envs (see above)

# Run with gunicorn (reverse proxy with Nginx recommended)
web: gunicorn -w 2 -k gevent app:app
```

On platforms like Render/Railway/Heroku, the included `Procfile` is compatible:

```
web: gunicorn -w 2 -k gevent app:app
```

### Render / Railway Quick Deploy

1. Push repo to GitHub.
2. Create a new Web Service (Render) or Service (Railway):
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn -w 2 -k gevent app:app --bind 0.0.0.0:$PORT`
3. Set environment variables (at minimum): `SECRET_KEY`, `DATABASE_URL` (Postgres), `ADMIN_USERNAME`, `ADMIN_PASSWORD`.
4. (Optional) Add: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, backup/email, WhatsApp variables.
5. Enable auto-deploy on commit.
6. Open the generated URL; first login seeds admin if not present.

`DATABASE_URL` that starts with `postgres://` is auto-normalized to `postgresql://` by code in `app.py`.

### Netlify (Frontend Split) Overview

If you choose a static front-end on Netlify:

1. Deploy backend as above (Render/Railway) to obtain API base URL.
2. Create a `frontend/` directory with static HTML/JS calling your API (`/api/members`, etc.).
3. Add `netlify.toml` redirect proxying `/api/*` to backend.
4. Implement client-side auth (fetch POST `/login`, store session cookie or migrate to JWT).
5. Gradually migrate Jinja pages to pure JS-rendered views.

### Postgres Migrations

After switching from SQLite to Postgres, run:

```bash
FLASK_APP=app.py flask db upgrade
```

Ensure `DATABASE_URL` is set before executing migrations.

### Health Check Endpoint

Use `/onboarding/status` or create a lightweight custom route to assist platform health checks:

```python
@app.route('/health')
def health():
	return {'ok': True}
```

Add `@app.route('/health')` in `app.py` if required by your host.

## Google Sign-In Setup

- Authorized redirect URI: `http://localhost:5000/auth/google/callback` (and your production domain callback)
- Add your production origin and callback to the Google Cloud Console OAuth Client settings
- Set `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` in environment

## WhatsApp Cloud API

- Obtain `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` from Meta Developer settings
- Set `WHATSAPP_DEFAULT_COUNTRY_CODE` (e.g., `92`)
- Optional template reminders: set `WHATSAPP_TEMPLATE_FEE_REMINDER_NAME` and `WHATSAPP_TEMPLATE_LANG`

## Roles & Admin Endpoints

- First run seeds an admin using `ADMIN_USERNAME`/`ADMIN_PASSWORD`
- Users have roles: `admin`, `staff`
- Admin-only APIs are under `/admin/*` (settings, backups, audit verify, CSV import, WhatsApp tools, schedule run-now)

## Security Notes

- Always set a strong `SECRET_KEY` in production
- Prefer HTTPS; cookies default to secure unless `FLASK_SECURE_COOKIES=0`
- Configure a firewall/reverse proxy; limit admin endpoints to trusted users
