from flask import Flask, request, jsonify, render_template, send_file, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from datetime import datetime, timezone
import pandas as pd
import os
import requests
from werkzeug.security import generate_password_hash, check_password_hash
import werkzeug
# Compatibility shim: some werkzeug builds omit __version__ attribute which
# Flask's test client expects. Ensure it's present for tests and tooling.
if not hasattr(werkzeug, '__version__'):
    try:
        from importlib.metadata import version as _pkg_version
        werkzeug.__version__ = _pkg_version('werkzeug')
    except Exception:
        werkzeug.__version__ = '0'
import json
import hashlib
import secrets
from sqlalchemy import or_, func
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage
import zipfile
from io import BytesIO
from authlib.integrations.flask_client import OAuth
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAVE_APSCHEDULER = True
except Exception:
    HAVE_APSCHEDULER = False

# Optional Google Drive support
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    HAVE_GDRIVE = True
except Exception:
    HAVE_GDRIVE = False

# Optional PDF generation (member card)
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as _pdf_canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.lib.colors import HexColor
    HAVE_PDF = True
except Exception:
    HAVE_PDF = False

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, "gym.db")
# Support DATABASE_URL for production (e.g., Postgres). Fallback to local SQLite.
db_url = os.getenv('DATABASE_URL')
# Normalize postgres scheme and ensure SSL for Render external URLs
if db_url:
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)
    # Auto-append sslmode=require for Render external hostnames when missing
    try:
        if ('render.com' in db_url) and ('sslmode=' not in db_url):
            sep = '&' if '?' in db_url else '?'
            db_url = f"{db_url}{sep}sslmode=require"
    except Exception:
        pass
app.config['SQLALCHEMY_DATABASE_URI'] = db_url or f"sqlite:///{db_path}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
Migrate(app, db)

# Static uploads (member photos)
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Local backups directory
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)

# Load environment variables and configure secret key
load_dotenv(os.path.join(BASE_DIR, '.env'))
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-me')
# Cookie security settings
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
if os.getenv('FLASK_SECURE_COOKIES', '1') not in ('0', 'false', 'False'):
    app.config['SESSION_COOKIE_SECURE'] = True

# OAuth client
oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)


def _normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format (basic, assumes country code if missing)."""
    import re
    phone = re.sub(r'\D', '', phone or '')
    if not phone:
        return ''
    # If phone starts with '0', replace with default country code (from settings or '92')
    default_cc = get_setting('whatsapp_default_country_code') or '92'
    if phone.startswith('0'):
        phone = default_cc + phone[1:]
    elif not phone.startswith(default_cc):
        # If not starting with country code, prepend it
        phone = default_cc + phone
    return phone

def get_gym_name() -> str:
    try:
        s = Setting.query.filter_by(key='gym_name').first()  # type: ignore[name-defined]
        if s and (s.value or '').strip():
            return s.value
    except Exception:
        pass
    return 'ZAIDAN FITNESS RECORD'

# Basic security headers
@app.after_request
def set_security_headers(resp):
    try:
        csp = " ".join([
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://accounts.google.com https://www.gstatic.com",
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            "img-src 'self' data: blob: https:",
            "font-src 'self' https://cdn.jsdelivr.net",
            "connect-src 'self' https://accounts.google.com https://www.googleapis.com",
            "frame-src 'self' https://accounts.google.com",
            "frame-ancestors 'none'",
        ])
        resp.headers['Content-Security-Policy'] = csp
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Referrer-Policy'] = 'no-referrer'
        resp.headers['Permissions-Policy'] = 'geolocation=(), microphone=()'
        if os.getenv('ENABLE_HSTS', '0') in ('1','true','True'):
            resp.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains; preload'
    except Exception:
        pass
    return resp

def get_setting_json(key: str, default: dict | list | None = None):
    try:
        s = Setting.query.filter_by(key=key).first()  # type: ignore[name-defined]
        if s and s.value:
            return json.loads(s.value)
    except Exception:
        pass
    return default

def set_setting_json(key: str, value_obj):
    set_setting(key, json.dumps(value_obj))  # type: ignore[name-defined]


def find_member_image_url(member_id: int):
    for ext in ALLOWED_IMAGE_EXTS:
        rel = f"/static/uploads/member_{member_id}{ext}"
        abs_path = os.path.join(UPLOAD_FOLDER, f"member_{member_id}{ext}")
        if os.path.exists(abs_path):
            return rel
    return None

def _find_member_image_abs_path(member_id: int) -> str | None:
    for ext in ALLOWED_IMAGE_EXTS:
        abs_path = os.path.join(UPLOAD_FOLDER, f"member_{member_id}{ext}")
        if os.path.exists(abs_path):
            return abs_path
    return None

def _build_member_card_pdf_bytes(member: "Member") -> tuple[bool, bytes, str]:
    if not HAVE_PDF:
        return False, b"", "PDF generation library not installed"
    buf = BytesIO()
    page_w, page_h = A4
    c = _pdf_canvas.Canvas(buf, pagesize=A4)
    # Card layout
    margin = 36
    card_w = page_w - margin * 2
    card_h = 220
    card_x = margin
    card_y = page_h - margin - card_h
    # Background
    c.setFillColor(HexColor("#111827"))
    c.roundRect(card_x, card_y, card_w, card_h, 12, fill=1, stroke=0)
    # Accent bar
    c.setFillColor(HexColor("#2563EB"))
    c.roundRect(card_x, card_y + card_h - 24, card_w, 24, 12, fill=1, stroke=0)
    # Title
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(card_x + 16, card_y + card_h - 18, f"{get_gym_name()} - MEMBER CARD")
    # Photo area
    img_path = _find_member_image_abs_path(member.id)
    img_size = 120
    img_x = card_x + 16
    img_y = card_y + card_h - 24 - 16 - img_size
    if img_path:
        try:
            c.drawImage(ImageReader(img_path), img_x, img_y, width=img_size, height=img_size, preserveAspectRatio=True, mask='auto')
        except Exception:
            # draw placeholder
            c.setFillColor(HexColor("#374151"))
            c.rect(img_x, img_y, img_size, img_size, fill=1, stroke=0)
    else:
        c.setFillColor(HexColor("#374151"))
        c.rect(img_x, img_y, img_size, img_size, fill=1, stroke=0)
    # Details
    text_x = img_x + img_size + 20
    text_y = card_y + card_h - 50
    c.setFillColor(HexColor("#E5E7EB"))
    c.setFont("Helvetica", 12)
    serial = 1000 + (member.id or 0)
    c.drawString(text_x, text_y, f"Name: {member.name}")
    c.drawString(text_x, text_y - 20, f"Serial: #{serial}")
    c.drawString(text_x, text_y - 40, f"Phone: {member.phone or ''}")
    c.drawString(text_x, text_y - 60, f"Admission Date: {member.admission_date.isoformat()}")
    # Footer / issued date
    c.setFillColor(HexColor("#60A5FA"))
    c.setFont("Helvetica-Oblique", 10)
    c.drawString(card_x + 16, card_y + 12, f"Issued: {datetime.now().strftime('%Y-%m-%d')}")
    c.showPage()
    c.save()
    buf.seek(0)
    return True, buf.read(), f"card_member_{member.id}.pdf"


def _save_member_image(member_id: int, storage) -> tuple[bool, str]:
    filename = storage.filename or ''
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return False, 'Only JPG, JPEG, PNG, WEBP images are allowed'
    # Remove any existing images for this member (any ext)
    for e in ALLOWED_IMAGE_EXTS:
        existing = os.path.join(UPLOAD_FOLDER, f"member_{member_id}{e}")
        try:
            if os.path.exists(existing):
                os.remove(existing)
        except Exception:
            pass
    dest = os.path.join(UPLOAD_FOLDER, f"member_{member_id}{ext}")
    storage.save(dest)
    return True, f"/static/uploads/member_{member_id}{ext}"

class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    phone = db.Column(db.String(50), index=True)
    admission_date = db.Column(db.Date, nullable=False)
    plan_type = db.Column(db.String(20), default='monthly')
    referral_code = db.Column(db.String(32), unique=True, index=True)
    referred_by = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=True)
    access_tier = db.Column(db.String(20), default='standard')  # standard/unlimited
    email = db.Column(db.String(255), nullable=True)
    training_type = db.Column(db.String(30), default='standard')  # standard/personal/cardio
    special_tag = db.Column(db.Boolean, default=False)
    custom_training = db.Column(db.String(50), nullable=True)
    monthly_fee = db.Column(db.Float, nullable=True)
    last_contact_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)  # Member active/inactive status

    def to_dict(self):
        # Compute current month fee status and last recorded amount
        now = datetime.now()
        try:
            p = Payment.query.filter_by(member_id=self.id, year=now.year, month=now.month).first()
            status = p.status if p else 'Unpaid'
        except Exception:
            status = 'Unpaid'
        try:
            tx = PaymentTransaction.query.filter_by(
                member_id=self.id,
                year=now.year,
                month=now.month
            ).order_by(PaymentTransaction.created_at.desc()).first()
            last_amt = float(tx.amount) if tx and tx.amount is not None else None
            last_time = tx.created_at.isoformat() if tx and tx.created_at else None
        except Exception:
            last_amt = None
            last_time = None
        try:
            monthly_price = float(get_setting('monthly_price') or '0')
        except Exception:
            monthly_price = 0.0
        display_training = None
        if self.custom_training and self.custom_training.strip():
            display_training = self.custom_training.strip()
        else:
            tt = (self.training_type or 'standard')
            if tt in ('standard', 'gym'): display_training = 'Gym'
            elif tt == 'personal': display_training = 'Personal'
            elif tt == 'cardio': display_training = 'Cardio'
            else: display_training = tt
        return {
            "id": self.id,
            "serial": 1000 + (self.id or 0),
            "name": self.name,
            "phone": self.phone,
            "phone_normalized": _normalize_phone(self.phone or ''),
            "admission_date": self.admission_date.isoformat(),
            "image_url": find_member_image_url(self.id) if self.id else None,
            "plan_type": self.plan_type or 'monthly',
            "referral_code": self.referral_code or '',
            "referred_by": self.referred_by,
            "access_tier": self.access_tier or 'standard',
            "email": self.email or '',
            "training_type": self.training_type or 'standard',
            "special_tag": bool(self.special_tag),
            "current_fee_status": status,
            "current_fee_amount": last_amt,
            "last_tx_time": last_time,
            "last_tx_amount": last_amt,
            "monthly_price": monthly_price,
            "custom_training": self.custom_training or '',
            "monthly_fee": self.monthly_fee,
            "display_training_type": display_training,
            "last_contact_at": self.last_contact_at.isoformat() if self.last_contact_at else None,
            "is_active": bool(self.is_active) if hasattr(self, 'is_active') else True,
        }

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)  # 1-12
    status = db.Column(db.String(20), nullable=False, index=True)  # Paid/Unpaid/N/A
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        db.Index('idx_payment_year_month_status', 'year', 'month', 'status'),
    )
    
    def to_dict(self):
        return {
            'id': self.id,
            'member_id': self.member_id,
            'year': self.year,
            'month': self.month,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

# Duplicate removed: to_dict
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    role = db.Column(db.String(20), default='staff')  # admin or staff

class OAuthAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(50), nullable=False)
    provider_id = db.Column(db.String(255), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class PaymentTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    plan_type = db.Column(db.String(20), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=True)
    amount = db.Column(db.Float, nullable=True)
    method = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(1000), nullable=True)

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    action = db.Column(db.String(100), nullable=False)
    data_json = db.Column(db.Text, nullable=False)
    prev_hash = db.Column(db.String(64), nullable=True)
    hash = db.Column(db.String(64), nullable=False)

class UploadedFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False, unique=True)
    rows_count = db.Column(db.Integer, nullable=False, default=0)
    rows_json = db.Column(db.Text, nullable=True)  # JSON snapshot of rows (truncated)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)
    stock = db.Column(db.Integer, nullable=False, default=0)
    category = db.Column(db.String(80), nullable=True)
    sku = db.Column(db.String(60), unique=True, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Attendance(db.Model):
    """Track member check-ins/check-outs"""
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    check_in = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    check_out = db.Column(db.DateTime, nullable=True)
    date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "member_id": self.member_id,
            "check_in": self.check_in.isoformat() if self.check_in else None,
            "check_out": self.check_out.isoformat() if self.check_out else None,
            "date": self.date.isoformat(),
            "notes": self.notes,
            "duration_minutes": self.get_duration_minutes()
        }
    
    def get_duration_minutes(self):
        if self.check_out and self.check_in:
            return int((self.check_out - self.check_in).total_seconds() / 60)
        return None

# Duplicate removed: to_dict
class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(32), unique=True, nullable=False)
    customer_name = db.Column(db.String(120), nullable=True)
    subtotal = db.Column(db.Float, nullable=False, default=0.0)
    tax = db.Column(db.Float, nullable=False, default=0.0)
    discount = db.Column(db.Float, nullable=False, default=0.0)
    total = db.Column(db.Float, nullable=False, default=0.0)
    payment_method = db.Column(db.String(40), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    channel = db.Column(db.String(30), nullable=False, default='pos')
    status = db.Column(db.String(30), nullable=False, default='paid')
    verification_hash = db.Column(db.String(64), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    synced_from_offline = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship('SaleItem', backref='sale', cascade='all, delete-orphan')

# Duplicate removed: to_dict
class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    name = db.Column(db.String(140), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0.0)
    total_price = db.Column(db.Float, nullable=False, default=0.0)

# Duplicate removed: to_dict
class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    username = db.Column(db.String(120), nullable=False)
    method = db.Column(db.String(30), nullable=False)
    ip_address = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

def get_setting(key: str, default: str | None = None) -> str | None:
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default

def set_setting(key: str, value: str) -> None:
    s = Setting.query.filter_by(key=key).first()
    if not s:
        s = Setting(key=key, value=value)
        db.session.add(s)
    else:
        s.value = value
    db.session.commit()

def _sql_column_exists(table: str, column: str) -> bool:
    try:
        res = db.session.execute(db.text(f"PRAGMA table_info('{table}')")).mappings().all()
        for row in res:
            if row.get('name') == column:
                return True
    except Exception:
        pass
    return False

def _ensure_schema():
    db.create_all()
    try:
        if not _sql_column_exists('member', 'plan_type'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN plan_type TEXT DEFAULT 'monthly'"))
        if not _sql_column_exists('member', 'referral_code'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN referral_code TEXT"))
        if not _sql_column_exists('member', 'referred_by'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN referred_by INTEGER"))
        if not _sql_column_exists('member', 'access_tier'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN access_tier TEXT DEFAULT 'standard'"))
        if not _sql_column_exists('user', 'role'):
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN role TEXT DEFAULT 'staff'"))
        if not _sql_column_exists('member', 'email'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN email TEXT"))
        if not _sql_column_exists('member', 'training_type'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN training_type TEXT DEFAULT 'standard'"))
        if not _sql_column_exists('member', 'special_tag'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN special_tag INTEGER DEFAULT 0"))
        if not _sql_column_exists('member', 'custom_training'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN custom_training TEXT"))
        if not _sql_column_exists('member', 'monthly_fee'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN monthly_fee REAL"))
        if not _sql_column_exists('member', 'last_contact_at'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN last_contact_at TEXT"))
        if not _sql_column_exists('member', 'is_active'):
            db.session.execute(db.text("ALTER TABLE member ADD COLUMN is_active INTEGER DEFAULT 1"))
        db.session.commit()
    except Exception:
        db.session.rollback()

def _hash_bytes(data: bytes) -> str:
    h = hashlib.sha256(); h.update(data); return h.hexdigest()

def _gen_referral_code(prefix: str = 'M') -> str:
    code = f"{prefix}{secrets.token_hex(3)}"
    while Member.query.filter_by(referral_code=code).first() is not None:
        code = f"{prefix}{secrets.token_hex(3)}"
    return code


def _generate_invoice_number() -> str:
    prefix = os.getenv('POS_INVOICE_PREFIX', 'INV')
    day = datetime.now(timezone.utc).strftime('%Y%m%d')
    while True:
        suffix = secrets.token_hex(2).upper()
        candidate = f"{prefix}{day}-{suffix}"
        if not Sale.query.filter_by(invoice_number=candidate).first():
            return candidate


def _sale_verification_hash(invoice: str, total: float) -> str:
    h = hashlib.sha256()
    h.update((invoice or '').encode('utf-8'))
    h.update(str(total or 0).encode('utf-8'))
    h.update((app.config.get('SECRET_KEY') or 'secret').encode('utf-8'))
    return h.hexdigest()


def _log_login_event(user: "User", method: str) -> None:
    try:
        entry = LoginLog(
            user_id=user.id if user else None,
            username=user.username if user else 'unknown',
            method=method,
            ip_address=request.remote_addr,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _sale_to_csv_bytes(sale: Sale) -> bytes:
    rows = ["invoice_number,customer,total,payment_method,created_at"]
    rows.append(
        f"{sale.invoice_number},{(sale.customer_name or '').replace(',', ' ')},{sale.total},{sale.payment_method or ''},{sale.created_at.isoformat()}"
    )
    rows.append("item,qty,price,total")
    for item in sale.items:
        rows.append(
            f"{item.name.replace(',', ' ')},{item.quantity},{item.unit_price},{item.total_price}"
        )
    return "\n".join(rows).encode('utf-8')


def _upload_backup_to_gdrive(data: bytes, filename: str, mime: str = 'application/octet-stream') -> tuple[bool, str]:
    """Uploads a file to Google Drive backup folder if enabled and configured."""
    if not HAVE_GDRIVE:
        return False, 'Google API client not installed'
    folder_id = os.getenv('GDRIVE_BACKUP_FOLDER_ID')
    sa_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')
    if not (folder_id and sa_file and os.path.exists(sa_file)):
        return False, 'Google Drive backup configuration missing'
    try:
        scopes = ['https://www.googleapis.com/auth/drive.file']
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
        service = build('drive', 'v3', credentials=creds)
        media = MediaIoBaseUpload(BytesIO(data), mimetype=mime, resumable=False)
        file_metadata = {
            'name': filename,
            'parents': [folder_id],
        }
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return True, f"Uploaded to Drive with file id {file.get('id')}"
    except Exception as exc:
        return False, str(exc)

def _append_sale_to_sheet(sale: Sale) -> tuple[bool, str | dict]:
    if not HAVE_GDRIVE:
        return False, 'Google API client not installed'
    sheet_id = os.getenv('SALES_SHEET_ID')
    sa_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')
    if not (sheet_id and sa_file and os.path.exists(sa_file)):
        return False, 'Sheet configuration missing'
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
        service = build('sheets', 'v4', credentials=creds)
        payload = {
            'values': [[
                sale.invoice_number,
                sale.created_at.isoformat() if sale.created_at else '',
                sale.customer_name or '',
                sale.total,
                sale.payment_method or '',
                json.dumps([item.to_dict() for item in sale.items]),
            ]]
        }
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=os.getenv('SALES_SHEET_RANGE', 'Sheet1!A:F'),
            valueInputOption='USER_ENTERED',
            body=payload,
        ).execute()
        return True, 'appended'
    except Exception as exc:
        return False, str(exc)

def _upload_sale_artifacts_to_drive(sale: Sale) -> dict:
    results = {}
    csv_bytes = _sale_to_csv_bytes(sale)
    ok, info = _upload_backup_to_gdrive(csv_bytes, f"sale_{sale.invoice_number}.csv", mime='text/csv')
    results['csv'] = {'ok': ok, 'info': info}
    sheet_ok, sheet_info = _append_sale_to_sheet(sale)
    results['sheet'] = {'ok': sheet_ok, 'info': sheet_info}
    return results


def _persist_sale(payload: dict, user_id: int | None, synced_offline: bool = False) -> tuple[Sale, dict]:
    items = payload.get('items') or []
    if not items:
        raise ValueError('Cart is empty')
    try:
        tax = float(payload.get('tax') or 0)
    except Exception:
        tax = 0.0
    try:
        discount = float(payload.get('discount') or 0)
    except Exception:
        discount = 0.0
    subtotal = 0.0
    normalized_items = []
    for row in items:
        qty_raw = row.get('quantity', 1)
        try:
            qty = max(1, int(qty_raw))
        except Exception:
            qty = 1
        price_raw = row.get('price') if row.get('price') not in (None, '') else row.get('unit_price')
        try:
            price = float(price_raw or 0.0)
        except Exception:
            price = 0.0
        subtotal += qty * price
        normalized_items.append({
            'product_id': row.get('product_id') or row.get('id'),
            'name': row.get('name') or 'Item',
            'quantity': qty,
            'price': price,
        })
    total = subtotal + tax - discount
    invoice = payload.get('invoice_number') or _generate_invoice_number()
    try:
        sale = Sale(
            invoice_number=invoice,
            customer_name=(payload.get('customer_name') or '').strip() or None,
            subtotal=subtotal,
            tax=tax,
            discount=discount,
            total=total,
            payment_method=(payload.get('payment_method') or 'cash').lower(),
            note=(payload.get('note') or '').strip() or None,
            channel=payload.get('channel') or ('offline' if synced_offline else 'pos'),
            status=payload.get('status') or 'paid',
            verification_hash=_sale_verification_hash(invoice, total),
            user_id=user_id,
            synced_from_offline=synced_offline,
        )
        db.session.add(sale)
        db.session.flush()
        for row in normalized_items:
            item = SaleItem(
                sale_id=sale.id,
                product_id=row['product_id'],
                name=row['name'],
                quantity=row['quantity'],
                unit_price=row['price'],
                total_price=row['price'] * row['quantity'],
            )
            db.session.add(item)
            if row['product_id']:
                product = db.session.get(Product, row['product_id'])
                if product:
                    product.stock = max(0, product.stock - row['quantity'])
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    backup_results = {}
    if os.getenv('POS_GOOGLE_BACKUP_ENABLED', '1') not in ('0', 'false', 'False'):
        try:
            backup_results = _upload_sale_artifacts_to_drive(sale)
        except Exception as exc:
            backup_results = {'error': str(exc)}
    append_audit('pos.sale.create', {
        'sale_id': sale.id,
        'invoice_number': sale.invoice_number,
        'total': sale.total,
        'user_id': user_id,
        'synced_from_offline': synced_offline,
    })
    return sale, backup_results


def _calculate_sales_snapshot() -> dict:
    today = datetime.now(timezone.utc).date()
    start_of_month = today.replace(day=1)
    today_total = (
        db.session.query(func.coalesce(func.sum(Sale.total), 0.0))
        .filter(func.date(Sale.created_at) == today)
        .scalar()
        or 0.0
    )
    month_total = (
        db.session.query(func.coalesce(func.sum(Sale.total), 0.0))
        .filter(Sale.created_at >= start_of_month)
        .scalar()
        or 0.0
    )
    return {'today_total': round(float(today_total), 2), 'month_total': round(float(month_total), 2)}


def _best_selling_products(limit: int = 5) -> list[dict]:
    rows = (
        db.session.query(
            SaleItem.name.label('name'),
            func.coalesce(func.sum(SaleItem.quantity), 0).label('qty'),
            func.coalesce(func.sum(SaleItem.total_price), 0.0).label('revenue'),
        )
        .group_by(SaleItem.name)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            'name': row.name,
            'quantity': int(row.qty or 0),
            'revenue': round(float(row.revenue or 0.0), 2),
        }
        for row in rows
    ]

def _audit_hash(prev: str | None, ts: str, action: str, data_json: str) -> str:
    h = hashlib.sha256()
    h.update((prev or '').encode('utf-8'))
    h.update(ts.encode('utf-8'))
    h.update(action.encode('utf-8'))
    h.update(data_json.encode('utf-8'))
    return h.hexdigest()

def append_audit(action: str, data: dict) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    prev = AuditLog.query.order_by(AuditLog.id.desc()).first()
    prev_hash = prev.hash if prev else None
    data_json = json.dumps(data, separators=(',', ':'), sort_keys=True)
    digest = _audit_hash(prev_hash, ts, action, data_json)
    rec = AuditLog(created_at=datetime.now(timezone.utc), action=action, data_json=data_json, prev_hash=prev_hash, hash=digest)
    db.session.add(rec)
    db.session.commit()

@app.before_first_request
def create_tables():
    _ensure_schema()
    # Seed an admin user if none exists
    admin_username = os.getenv('ADMIN_USERNAME', 'admin')
    admin_password = os.getenv('ADMIN_PASSWORD', 'admin123')
    existing_admin = User.query.filter_by(username=admin_username).first()
    if not existing_admin:
        admin_user = User(username=admin_username, password_hash=generate_password_hash(admin_password), role='admin')
        db.session.add(admin_user)
        db.session.commit()
    if get_setting('gym_name') is None:
        set_setting('gym_name', 'ZAIDAN FITNESS RECORD')
    # Start scheduler once
    start_scheduler_once()
    # Optional immediate rollover on startup if enabled
    if os.getenv('AUTO_PAYMENT_ROLLOVER_ENABLED', '0') not in ('0','false','False',''):
        try:
            payment_rollover_job()
        except Exception:
            pass

def start_scheduler_once():
    if app.config.get('SCHEDULER_STARTED'):
        return
    if not HAVE_APSCHEDULER:
        app.config['SCHEDULER_STARTED'] = True
        return
    enabled = os.getenv('SCHEDULE_REMINDERS_ENABLED', '0') not in ('0','false','False','')
    if not enabled:
        app.config['SCHEDULER_STARTED'] = True
        return
    hour = int(os.getenv('SCHEDULE_TIME_HH', '9'))
    minute = int(os.getenv('SCHEDULE_TIME_MM', '0'))
    # Avoid duplicate on Flask reloader
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        scheduler = BackgroundScheduler()
        trigger = CronTrigger(hour=hour, minute=minute)
        scheduler.add_job(send_monthly_unpaid_template_job, trigger)
        # Optional: payment rollover (ensure current year rows)
        if os.getenv('AUTO_PAYMENT_ROLLOVER_ENABLED', '0') not in ('0','false','False',''):
            rollover_hour = int(os.getenv('ROLLOVER_TIME_HH', '2'))
            rollover_minute = int(os.getenv('ROLLOVER_TIME_MM', '15'))
            scheduler.add_job(payment_rollover_job, CronTrigger(hour=rollover_hour, minute=rollover_minute))
        scheduler.start()
    app.config['SCHEDULER_STARTED'] = True

def send_monthly_unpaid_template_job():
    with app.app_context():
        now = datetime.now()
        try:
            # Prefer template reminders if configured; otherwise fall back to text reminders
            if os.getenv('WHATSAPP_TEMPLATE_FEE_REMINDER_NAME'):
                _ = send_bulk_template_reminders(now.year, now.month)
            else:
                # Fallback: send simple WhatsApp text reminders to unpaid members
                unpaid = Payment.query.filter_by(year=now.year, month=now.month, status='Unpaid').all()
                for p in unpaid:
                    m = db.session.get(Member, p.member_id)
                    if not m:
                        continue
                    phone = _normalize_phone(m.phone or '')
                    if not phone:
                        continue
                    month_name = now.strftime('%B')
                    text = f"Dear {m.name}, your {month_name} {now.year} gym fee is unpaid. Please pay as soon as possible. Thank you!"
                    send_whatsapp_text(phone, text)
        except Exception:
            pass

# Simple session-based login protection (supports PUBLIC_MODE)
def login_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        # Allow public, read-only access when PUBLIC_MODE is enabled
        try:
            public_mode = os.getenv('PUBLIC_MODE', '0').lower() in ('1', 'true', 'yes')
        except Exception:
            public_mode = False
        if public_mode:
            return view_func(*args, **kwargs)
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return view_func(*args, **kwargs)
    return wrapper

def admin_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        try:
            uid = session.get('user_id')
            user = db.session.get(User, uid) if uid else None
            is_admin = bool(user and (user.role or 'staff') == 'admin')
        except Exception:
            is_admin = False
        if not is_admin:
            wants_json = request.path.startswith('/api') or 'application/json' in (request.headers.get('Accept') or '')
            if wants_json:
                return jsonify({'ok': False, 'error': 'Admin only'}), 403
            return redirect(url_for('dashboard'))
        return view_func(*args, **kwargs)
    return wrapper

def payment_rollover_job():
    with app.app_context():
        year = datetime.now().year
        try:
            members = Member.query.all()
            for m in members:
                ensure_payment_rows(m, year)
        except Exception:
            pass
    return jsonify({'ok': True})

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        # basic validations
        if len(username) < 3:
            flash('Username must be at least 3 characters', 'warning')
            return render_template('register.html')
        if password != confirm:
            flash('Passwords do not match', 'warning')
            return render_template('register.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters', 'warning')
            return render_template('register.html')
        if User.query.filter_by(username=username).first():
            flash('Username is already taken', 'warning')
            return render_template('register.html')

        user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        session['user_id'] = user.id
        session['username'] = user.username
        return redirect(url_for('dashboard'))
    return render_template('register.html', gym_name=get_gym_name())

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page endpoint referenced by auth redirects.
    Supports Google OAuth and local username/password.
    """
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        if not username or not password:
            flash('Username and password are required', 'warning')
            return render_template('login.html', gym_name=get_gym_name())
        user = User.query.filter_by(username=username).first()
        try:
            from werkzeug.security import check_password_hash
            valid = bool(user and check_password_hash(user.password_hash or '', password))
        except Exception:
            valid = False
        if not valid:
            flash('Invalid credentials', 'danger')
            return render_template('login.html', gym_name=get_gym_name())
        session['user_id'] = user.id
        session['username'] = user.username
        _log_login_event(user, 'local-password')
        next_url = request.args.get('next') or url_for('dashboard')
        return redirect(next_url)
    # GET
    try:
        return render_template('login.html', gym_name=get_gym_name())
    except Exception:
        # Minimal fallback when template is missing
        return (
            '<html><body style="font-family:sans-serif">'
            '<h3>Login</h3>'
            '<form method="post">'
            '<input name="username" placeholder="Username" />'
            '<input name="password" type="password" placeholder="Password" />'
            '<button type="submit">Sign In</button>'
            '</form>'
            '<p style="margin-top:10px"><a href="/login/google">Continue with Google</a></p>'
            '</body></html>'
        )

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/offline')
def offline():
    return render_template('offline.html')

@app.route('/manifest.json')
def manifest():
    return send_file(os.path.join(BASE_DIR, 'static', 'manifest.json'), mimetype='application/json')

@app.route('/dashboard')
@login_required
def dashboard():
    total_members = Member.query.count()
    now = datetime.now()
    paid_count = Payment.query.filter_by(year=now.year, month=now.month, status='Paid').count()
    unpaid_count = Payment.query.filter_by(year=now.year, month=now.month, status='Unpaid').count()
    na_count = Payment.query.filter_by(year=now.year, month=now.month, status='N/A').count()
    recent_members = Member.query.order_by(Member.id.desc()).limit(5).all()
    currency_code = get_setting('currency_code') or 'USD'
    monthly_price = get_setting('monthly_price') or '8'
    logo_url = get_setting('logo_filename') or ''
    default_cc = get_setting('whatsapp_default_country_code') or '92'
    # Expose admin flag for template to gate admin-only actions
    is_admin = False
    try:
        uid = session.get('user_id')
        user = db.session.get(User, uid) if uid else None
        is_admin = bool(user and (user.role or 'staff') == 'admin')
    except Exception:
        is_admin = False
    return render_template(
        'dashboard.html',
        total_members=total_members,
        month=now.month,
        year=now.year,
        paid_count=paid_count,
        unpaid_count=unpaid_count,
        na_count=na_count,
        recent_members=recent_members,
        username=session.get('username'),
        gym_name=get_gym_name(),
        currency_code=currency_code,
        monthly_price=monthly_price,
        logo_url=logo_url,
        default_cc=default_cc,
        is_admin=is_admin
    )

@app.route('/dashboard/excel')
@login_required
def excel_dashboard():
    """Excel data management dashboard."""
    gym_name = get_gym_name()
    logo_url = get_setting('logo_filename') or ''
    return render_template('excel_dashboard.html', gym_name=gym_name, logo_url=logo_url, username=session.get('username'))

@app.route('/dashboard/analytics')
@login_required
def analytics_dashboard():
    """Advanced analytics dashboard."""
    gym_name = get_gym_name()
    logo_url = get_setting('logo_filename') or ''
    return render_template('analytics.html', gym_name=gym_name, logo_url=logo_url, username=session.get('username'))

@app.route('/api/stats/monthly')
@login_required
def stats_monthly():
    try:
        year = int(request.args.get('year') or datetime.now().year)
    except ValueError:
        return jsonify({"error":"invalid year"}), 400
    paid = []
    unpaid = []
    na = []
    for m in range(1, 12+1):
        paid.append(Payment.query.filter_by(year=year, month=m, status='Paid').count())
        unpaid.append(Payment.query.filter_by(year=year, month=m, status='Unpaid').count())
        na.append(Payment.query.filter_by(year=year, month=m, status='N/A').count())
    return jsonify({"year": year, "paid": paid, "unpaid": unpaid, "na": na})

@app.route('/login/google')
def login_google():
    redirect_uri = url_for('auth_google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route('/auth/google/callback')
def auth_google_callback():
    token = oauth.google.authorize_access_token()
    # Fetch userinfo using the userinfo endpoint
    resp = oauth.google.get('userinfo')
    info = resp.json() if resp else {}
    sub = info.get('sub') or (token.get('userinfo') or {}).get('sub')
    email = info.get('email') or ''
    name = info.get('name') or email or 'user'

    if not sub:
        flash('Google login failed: no subject identifier', 'danger')
        return redirect(url_for('login'))

    acct = OAuthAccount.query.filter_by(provider='google', provider_id=sub).first()
    if acct:
        user = db.session.get(User, acct.user_id)
    else:
        # Create a new user and link OAuth account
        # Ensure unique username; prefer email if available
        base_username = email or name.replace(' ', '').lower()
        username = base_username or f'user{sub[:6]}'
        # Make sure username is unique
        suffix = 1
        orig = username
        while User.query.filter_by(username=username).first() is not None:
            suffix += 1
            username = f"{orig}{suffix}"
        user = User(username=username, password_hash=generate_password_hash(os.urandom(16).hex()))
        db.session.add(user)
        db.session.commit()
        acct = OAuthAccount(provider='google', provider_id=sub, user_id=user.id)
        db.session.add(acct)
        db.session.commit()

    session['user_id'] = user.id
    session['username'] = user.username
    _log_login_event(user, 'google-oauth')
    # Optionally trigger backup on login (function not implemented)
    # try:
    #     trigger_backup_on_login()
    # except Exception:
    #     pass
    next_url = request.args.get('next') or url_for('dashboard')
    return redirect(next_url)


# Common aliases to reduce 404s when typing URLs


# Members page (requires login) - keeps existing template
@app.route('/members')
@login_required
def index():
    members = Member.query.all()
    currency_code = get_setting('currency_code') or 'USD'
    monthly_price = get_setting('monthly_price') or '8'
    logo_url = get_setting('logo_filename') or ''
    default_cc = get_setting('whatsapp_default_country_code') or '92'
    return render_template('index.html', members=members, gym_name=get_gym_name(), currency_code=currency_code, monthly_price=monthly_price, logo_url=logo_url, default_cc=default_cc)

## Duplicate block removed: add_member (kept first definition above)

## Duplicate block removed: list_members (kept first definition above)

# Duplicate removed: get_member (kept first definition above)


# Duplicate removed: delete_member (kept first definition above)

# Duplicate removed: update_member (kept first definition above)
    plan_type = (data.get('plan_type') or '').lower().strip()
    if plan_type in ('monthly','yearly'):
        m.plan_type = plan_type; changed['plan_type'] = plan_type
    access_tier = (data.get('access_tier') or '').lower().strip()
    if access_tier in ('standard','unlimited'):
        m.access_tier = access_tier; changed['access_tier'] = access_tier
    email = (data.get('email') or '').strip()
    if email:
        m.email = email; changed['email'] = email
    training_type = (data.get('training_type') or '').lower().strip()
    if training_type in ('standard','personal','cardio','other'):
        if training_type == 'other':
            training_type = 'standard'
        m.training_type = training_type; changed['training_type'] = training_type
    if 'custom_training' in data:
        m.custom_training = (data.get('custom_training') or '').strip() or None; changed['custom_training'] = m.custom_training
    if 'monthly_fee' in data:
        try:
            val = data.get('monthly_fee')
            if val not in (None, ''):
                m.monthly_fee = float(val); changed['monthly_fee'] = m.monthly_fee
        except Exception:
            pass
    if 'special_tag' in data:
        m.special_tag = bool(data.get('special_tag')); changed['special_tag'] = bool(data.get('special_tag'))
    # Duplicate removed: end of update_member block


# Duplicate removed: upload_member_photo (kept first definition above)

# API: get member payments
# Duplicate removed: get_payments (kept first definition above)

# API: update payment (mark paid/unpaid)
# Duplicate removed: update_payment (kept first definition above)

# Export member payments to excel
# Duplicate removed: update_member (kept first definition above)
@login_required
def set_member_plan(member_id):
    m = Member.query.get_or_404(member_id)
    data = request.json or {}
    plan = (data.get('plan_type') or '').lower()
    if plan not in ('monthly','yearly'):
        return jsonify({'error': 'plan_type must be monthly or yearly'}), 400
    m.plan_type = plan
    db.session.commit()
    append_audit('member.plan.update', {'member_id': m.id, 'plan_type': plan, 'user_id': session.get('user_id')})
    return jsonify({'ok': True, 'member': m.to_dict()})

@app.route('/api/members/<int:member_id>/pay', methods=['POST'])
@login_required
def record_payment(member_id):
    m = Member.query.get_or_404(member_id)
    data = request.json or {}
    year = int(data.get('year') or datetime.now().year)
    month = data.get('month')
    method = (data.get('method') or '').strip() or None
    amount = data.get('amount')
    plan = (data.get('plan_type') or m.plan_type or 'monthly').lower()
    if plan not in ('monthly','yearly'):
        plan = 'monthly'
    ensure_payment_rows(m, year)
    if plan == 'monthly':
        try:
            month = int(month or datetime.now().month)
        except Exception:
            return jsonify({'error': 'month required for monthly payment'}), 400
        p = Payment.query.filter_by(member_id=m.id, year=year, month=month).first()
        if not p:
            p = Payment(member_id=m.id, year=year, month=month, status='Paid')
            db.session.add(p)
        else:
            p.status = 'Paid'
        txn = PaymentTransaction(member_id=m.id, user_id=session.get('user_id'), plan_type='monthly', year=year, month=month, amount=amount, method=method)
        db.session.add(txn)
        db.session.commit()
        append_audit('payment.txn.monthly', {'member_id': m.id, 'year': year, 'month': month, 'amount': amount, 'method': method, 'user_id': session.get('user_id')})
        return jsonify({'ok': True})
    else:
        # yearly: mark all months Paid for specified year
        for mm in range(1, 13):
            p = Payment.query.filter_by(member_id=m.id, year=year, month=mm).first()
            if not p:
                db.session.add(Payment(member_id=m.id, year=year, month=mm, status='Paid'))
            else:
                p.status = 'Paid'
        txn = PaymentTransaction(member_id=m.id, user_id=session.get('user_id'), plan_type='yearly', year=year, month=None, amount=amount, method=method)
        db.session.add(txn)
        db.session.commit()
        append_audit('payment.txn.yearly', {'member_id': m.id, 'year': year, 'amount': amount, 'method': method, 'user_id': session.get('user_id')})
        return jsonify({'ok': True})

@app.route('/api/members/<int:member_id>/message', methods=['POST'])
@login_required
def message_member(member_id):
    m = Member.query.get_or_404(member_id)
    data = request.get_json(silent=True) or {}
    text = (data.get('message') or '').strip()
    phone_override = (data.get('phone') or '').strip() or None
    if not text:
        return jsonify({'ok': False, 'error': 'message required'}), 400
    phone = _normalize_phone(phone_override or m.phone or '')
    if not phone:
        return jsonify({'ok': False, 'error': 'member has no phone'}), 400
    ok, res = send_whatsapp_text(phone, text)
    if ok:
        m.last_contact_at = datetime.now(timezone.utc)
        db.session.commit()
        append_audit('member.message.send', {'member_id': m.id, 'phone': phone, 'user_id': session.get('user_id')})
    return jsonify({'ok': ok, 'result': res, 'member': m.to_dict()})

@app.route('/api/uploads', methods=['GET'])
@login_required
def list_uploads():
    files = UploadedFile.query.order_by(UploadedFile.id.desc()).all()
    return jsonify([
        {
            'id': f.id,
            'original_name': f.original_name,
            'stored_name': f.stored_name,
            'content_hash': f.content_hash,
            'rows_count': f.rows_count,
            'uploaded_at': f.uploaded_at.isoformat()
        } for f in files
    ])

@app.route('/api/uploads/<int:file_id>', methods=['GET'])
@login_required
def get_upload(file_id):
    f = UploadedFile.query.get_or_404(file_id)
    detail = {
        'id': f.id,
        'original_name': f.original_name,
        'stored_name': f.stored_name,
        'content_hash': f.content_hash,
        'rows_count': f.rows_count,
        'uploaded_at': f.uploaded_at.isoformat(),
    }
    # Provide rows snapshot (parsed) limited
    try:
        if f.rows_json:
            detail['rows'] = json.loads(f.rows_json)
    except Exception:
        detail['rows'] = []
    return jsonify(detail)

@app.route('/api/uploads/<int:file_id>', methods=['DELETE'])
@login_required
def delete_upload(file_id: int):
    """Delete an uploaded data file record and its stored file from disk."""
    f = UploadedFile.query.get_or_404(file_id)
    storage_dir = os.path.join(BASE_DIR, 'data_uploads')
    file_path = os.path.join(storage_dir, f.stored_name or '')
    # Remove DB record first to avoid partial deletes blocking
    db.session.delete(f)
    db.session.commit()
    # Best-effort file removal
    try:
        if f.stored_name and os.path.isfile(file_path):
            os.remove(file_path)
    except Exception:
        pass
    append_audit('data.upload.delete', {'file_id': file_id, 'user_id': session.get('user_id')})
    return jsonify({'ok': True, 'deleted_id': file_id})

@app.route('/api/uploads', methods=['POST'])
@login_required
def upload_data_file():
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'file field required'}), 400
    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'empty filename'}), 400
    orig = f.filename
    data = f.read()
    if not data:
        return jsonify({'ok': False, 'error': 'empty file'}), 400
    digest = _hash_bytes(data)
    # Duplicate content handling: return existing record instead of error
    existing_upload = UploadedFile.query.filter_by(content_hash=digest).first()
    if existing_upload:
        return jsonify({
            'ok': True,
            'duplicate': True,
            'file': {
                'id': existing_upload.id,
                'original_name': existing_upload.original_name,
                'rows_count': existing_upload.rows_count
            },
            'message': 'Duplicate file detected; using previously uploaded file.'
        })
    # Determine extension and parse
    ext = os.path.splitext(orig)[1].lower()
    parsed_rows = []
    rows_count = 0
    try:
        import pandas as pd  # local import
        if ext in ('.csv',):
            from io import StringIO
            df = pd.read_csv(StringIO(data.decode('utf-8', 'ignore')))
        elif ext in ('.xlsx', '.xls'):
            from io import BytesIO
            df = pd.read_excel(BytesIO(data))
        else:
            df = None
        if df is not None:
            rows_count = len(df.index)
            # snapshot max 50 rows
            snap = df.head(50)
            parsed_rows = snap.to_dict(orient='records')
            # Convert datetime objects to strings for JSON serialization
            for row in parsed_rows:
                for key, value in row.items():
                    if isinstance(value, (datetime, pd.Timestamp)):
                        row[key] = value.strftime('%Y-%m-%d') if value else None
                    elif pd.isna(value):
                        row[key] = None
    except Exception:
        pass
    stored_name = f"upload_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext or ''}"
    storage_dir = os.path.join(BASE_DIR, 'data_uploads')
    os.makedirs(storage_dir, exist_ok=True)
    with open(os.path.join(storage_dir, stored_name), 'wb') as out:
        out.write(data)
    rec = UploadedFile(original_name=orig, stored_name=stored_name, content_hash=digest, rows_count=rows_count, rows_json=json.dumps(parsed_rows) if parsed_rows else None)
    db.session.add(rec)
    db.session.commit()
    append_audit('data.upload', {'file_id': rec.id, 'original_name': orig, 'rows_count': rows_count, 'user_id': session.get('user_id')})
    return jsonify({'ok': True, 'file': {'id': rec.id, 'original_name': rec.original_name, 'rows_count': rec.rows_count}})


# POS features removed

@app.route('/fees')
@login_required
def fees_page():
    currency_code = get_setting('currency_code') or 'USD'
    monthly_price = get_setting('monthly_price') or '8'
    logo_url = get_setting('logo_filename') or ''
    default_cc = get_setting('whatsapp_default_country_code') or '92'
    return render_template(
        'fees.html',
        gym_name=get_gym_name(),
        currency_code=currency_code,
        monthly_price=monthly_price,
        logo_url=logo_url,
        default_cc=default_cc,
    )


@app.route('/api/fees', methods=['GET'])
@login_required
def fees_api():
    try:
        year = int(request.args.get('year') or datetime.now().year)
        month = int(request.args.get('month') or datetime.now().month)
    except ValueError:
        return jsonify({"error": "invalid year/month"}), 400
    members = Member.query.order_by(Member.id).all()
    results = []
    for m in members:
        ensure_payment_rows(m, year)
        p = Payment.query.filter_by(member_id=m.id, year=year, month=month).first()
        status = p.status if p else 'Unpaid'
        results.append({
            'member': m.to_dict(),
            'year': year,
            'month': month,
            'status': status
        })
    return jsonify(results)

@app.route('/api/fees/remind', methods=['POST'])
@login_required
def fees_remind():
    try:
        year = int(request.args.get('year') or datetime.now().year)
        month = int(request.args.get('month') or datetime.now().month)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid year/month"}), 400
        result = send_bulk_text_reminders(year, month)
        return jsonify(result)
    
    def send_bulk_text_reminders(year: int, month: int) -> dict:
        """Send WhatsApp text reminders to all unpaid members for the given year and month."""
        unpaid = Payment.query.filter_by(year=year, month=month, status='Unpaid').all()
        sent, failed = 0, 0
        for p in unpaid:
            m = db.session.get(Member, p.member_id)
            if not m:
                continue
            phone = _normalize_phone(m.phone or '')
            if not phone:
                failed += 1
                continue
            month_name = datetime(year, month, 1).strftime('%B')
            text = f"Dear {m.name}, your {month_name} {year} gym fee is unpaid. Please pay as soon as possible. Thank you!"
            ok, _ = send_whatsapp_text(phone, text)
            if ok:
                sent += 1
            else:
                failed += 1
        return {"ok": True, "sent": sent, "failed": failed}

@app.route('/api/backup/create', methods=['POST'])
@login_required
def backup_create():
    """Create a backup ZIP file with database and uploads."""
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"backup_{timestamp}.zip"
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, backup_filename)
        
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Add database file
            db_path = os.path.join(os.path.dirname(__file__), 'gym.db')
            if os.path.exists(db_path):
                zipf.write(db_path, 'gym.db')
            # Add uploads directory
            uploads_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
            if os.path.exists(uploads_dir):
                for root, dirs, files in os.walk(uploads_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.join('static', 'uploads', os.path.relpath(file_path, uploads_dir))
                        zipf.write(file_path, arcname)
        
        return jsonify({"ok": True, "filename": backup_filename, "path": backup_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/backup/download', methods=['GET'])
@login_required
def backup_download():
    """Download the most recent backup file."""
    try:
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        if not os.path.exists(backup_dir):
            return jsonify({"ok": False, "error": "No backups directory found"}), 404
        backups = [f for f in os.listdir(backup_dir) if f.endswith('.zip')]
        if not backups:
            return jsonify({"ok": False, "error": "No backups found"}), 404
        backups.sort(reverse=True)
        latest = backups[0]
        backup_path = os.path.join(backup_dir, latest)
        return send_file(backup_path, as_attachment=True, download_name=latest)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/backup/list', methods=['GET'])
@login_required
def backup_list():
    """List all available backup files."""
    try:
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        if not os.path.exists(backup_dir):
            return jsonify({"ok": True, "backups": []})
        backups = []
        for f in os.listdir(backup_dir):
            if f.endswith('.zip'):
                file_path = os.path.join(backup_dir, f)
                size = os.path.getsize(file_path)
                created = datetime.fromtimestamp(os.path.getctime(file_path)).isoformat()
                backups.append({"filename": f, "size": size, "created": created})
        backups.sort(key=lambda x: x['created'], reverse=True)
        return jsonify({"ok": True, "backups": backups})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/system/reset', methods=['POST'])
@login_required
def system_reset():
    """Reset system to factory defaults (clear all data)."""
    try:
        confirm = request.json.get('confirm')
        if confirm != 'RESET':
            return jsonify({"ok": False, "error": "Confirmation required"}), 400
        # Drop all tables and recreate
        db.drop_all()
        db.create_all()
        return jsonify({"ok": True, "message": "System reset to factory defaults"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ============= ADVANCED FEATURES =============

@app.route('/api/attendance/checkin', methods=['POST'])
@login_required
def attendance_checkin():
    """Check in a member"""
    try:
        member_id = request.json.get('member_id')
        notes = request.json.get('notes', '')
        
        if not member_id:
            return jsonify({"ok": False, "error": "member_id required"}), 400
        
        member = db.session.get(Member, member_id)
        if not member:
            return jsonify({"ok": False, "error": "Member not found"}), 404
        
        today = datetime.now().date()
        # Check if already checked in today
        existing = Attendance.query.filter_by(member_id=member_id, date=today).first()
        if existing and not existing.check_out:
            return jsonify({"ok": False, "error": "Already checked in today"}), 400
        
        attendance = Attendance(
            member_id=member_id,
            check_in=datetime.now(),
            date=today,
            notes=notes
        )
        db.session.add(attendance)
        db.session.commit()
        
        return jsonify({"ok": True, "attendance": attendance.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/attendance/checkout', methods=['POST'])
@login_required
def attendance_checkout():
    """Check out a member"""
    try:
        member_id = request.json.get('member_id')
        
        if not member_id:
            return jsonify({"ok": False, "error": "member_id required"}), 400
        
        today = datetime.now().date()
        attendance = Attendance.query.filter_by(
            member_id=member_id,
            date=today
        ).filter(Attendance.check_out.is_(None)).first()
        
        if not attendance:
            return jsonify({"ok": False, "error": "No active check-in found"}), 404
        
        attendance.check_out = datetime.now()
        db.session.commit()
        
        return jsonify({"ok": True, "attendance": attendance.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/attendance/today', methods=['GET'])
@login_required
def attendance_today():
    """Get today's attendance"""
    try:
        today = datetime.now().date()
        records = Attendance.query.filter_by(date=today).all()
        
        result = []
        for att in records:
            member = db.session.get(Member, att.member_id)
            data = att.to_dict()
            data['member'] = member.to_dict() if member else None
            result.append(data)
        
        return jsonify({"ok": True, "attendance": result, "count": len(result)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/attendance/history', methods=['GET'])
@login_required
def attendance_history():
    """Get attendance history for a member or date range"""
    try:
        member_id = request.args.get('member_id', type=int)
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        
        query = Attendance.query
        
        if member_id:
            query = query.filter_by(member_id=member_id)
        
        if date_from:
            query = query.filter(Attendance.date >= datetime.strptime(date_from, '%Y-%m-%d').date())
        
        if date_to:
            query = query.filter(Attendance.date <= datetime.strptime(date_to, '%Y-%m-%d').date())
        
        records = query.order_by(Attendance.check_in.desc()).limit(100).all()
        
        result = []
        for att in records:
            member = db.session.get(Member, att.member_id)
            data = att.to_dict()
            data['member_name'] = member.name if member else 'Unknown'
            result.append(data)
        
        return jsonify({"ok": True, "attendance": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/analytics/overview', methods=['GET'])
@login_required
def analytics_overview():
    """Get comprehensive analytics overview"""
    try:
        now = datetime.now()
        today = now.date()
        
        # Total members
        total_members = Member.query.count()
        active_members = Member.query.filter_by(is_active=True).count()
        
        # This month payments
        paid_this_month = Payment.query.filter_by(
            year=now.year, month=now.month, status='Paid'
        ).count()
        unpaid_this_month = Payment.query.filter_by(
            year=now.year, month=now.month, status='Unpaid'
        ).count()
        
        # Revenue this month
        revenue_this_month = db.session.query(func.sum(PaymentTransaction.amount)).filter(
            PaymentTransaction.year == now.year,
            PaymentTransaction.month == now.month
        ).scalar() or 0.0
        
        # Attendance today
        attendance_today = Attendance.query.filter_by(date=today).count()
        
        # New members this month
        new_members = Member.query.filter(
            func.extract('year', Member.admission_date) == now.year,
            func.extract('month', Member.admission_date) == now.month
        ).count()
        
        # Training type breakdown
        training_breakdown = {}
        for row in db.session.query(Member.training_type, func.count(Member.id)).group_by(Member.training_type).all():
            training_breakdown[row[0] or 'standard'] = row[1]
        
        # Revenue trend (last 6 months)
        revenue_trend = []
        for i in range(5, -1, -1):
            month_date = datetime(now.year, now.month, 1)
            if now.month - i < 1:
                month_date = datetime(now.year - 1, 12 + (now.month - i), 1)
            else:
                month_date = datetime(now.year, now.month - i, 1)
            
            month_revenue = db.session.query(func.sum(PaymentTransaction.amount)).filter(
                PaymentTransaction.year == month_date.year,
                PaymentTransaction.month == month_date.month
            ).scalar() or 0.0
            
            revenue_trend.append({
                "month": month_date.strftime('%b %Y'),
                "revenue": float(month_revenue)
            })
        
        return jsonify({
            "ok": True,
            "total_members": total_members,
            "active_members": active_members,
            "paid_this_month": paid_this_month,
            "unpaid_this_month": unpaid_this_month,
            "revenue_this_month": float(revenue_this_month),
            "attendance_today": attendance_today,
            "new_members_this_month": new_members,
            "training_breakdown": training_breakdown,
            "revenue_trend": revenue_trend
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/reminders/bulk', methods=['POST'])
@login_required
def reminders_bulk():
    """Send bulk reminders to selected members"""
    try:
        member_ids = request.json.get('member_ids', [])
        
        if not member_ids:
            return jsonify({"ok": False, "error": "No members selected"}), 400
        
        now = datetime.now()
        sent, failed = 0, 0
        
        for member_id in member_ids:
            member = db.session.get(Member, member_id)
            if not member:
                failed += 1
                continue
            
            phone = _normalize_phone(member.phone or '')
            if not phone:
                failed += 1
                continue
            
            # Check unpaid status
            payment = Payment.query.filter_by(
                member_id=member_id,
                year=now.year,
                month=now.month,
                status='Unpaid'
            ).first()
            
            if not payment:
                continue
            
            month_name = now.strftime('%B')
            text = f"Dear {member.name}, your {month_name} {now.year} gym fee is unpaid. Please pay as soon as possible. Thank you!"
            ok, _ = send_whatsapp_text(phone, text)
            
            if ok:
                sent += 1
            else:
                failed += 1
        
        return jsonify({"ok": True, "sent": sent, "failed": failed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/reports/monthly', methods=['GET'])
@login_required
def reports_monthly():
    """Generate monthly report"""
    try:
        year = int(request.args.get('year', datetime.now().year))
        month = int(request.args.get('month', datetime.now().month))
        
        # Payment stats
        paid = Payment.query.filter_by(year=year, month=month, status='Paid').count()
        unpaid = Payment.query.filter_by(year=year, month=month, status='Unpaid').count()
        na = Payment.query.filter_by(year=year, month=month, status='N/A').count()
        
        # Revenue
        revenue = db.session.query(func.sum(PaymentTransaction.amount)).filter(
            PaymentTransaction.year == year,
            PaymentTransaction.month == month
        ).scalar() or 0.0
        
        # Attendance stats
        month_start = datetime(year, month, 1).date()
        if month == 12:
            month_end = datetime(year + 1, 1, 1).date()
        else:
            month_end = datetime(year, month + 1, 1).date()
        
        total_attendance = Attendance.query.filter(
            Attendance.date >= month_start,
            Attendance.date < month_end
        ).count()
        
        unique_members = db.session.query(func.count(func.distinct(Attendance.member_id))).filter(
            Attendance.date >= month_start,
            Attendance.date < month_end
        ).scalar() or 0
        
        # Top attending members
        top_attendance = db.session.query(
            Attendance.member_id,
            func.count(Attendance.id).label('visits')
        ).filter(
            Attendance.date >= month_start,
            Attendance.date < month_end
        ).group_by(Attendance.member_id).order_by(func.count(Attendance.id).desc()).limit(10).all()
        
        top_members = []
        for att in top_attendance:
            member = db.session.get(Member, att.member_id)
            if member:
                top_members.append({
                    "name": member.name,
                    "visits": att.visits
                })
        
        return jsonify({
            "ok": True,
            "year": year,
            "month": month,
            "paid": paid,
            "unpaid": unpaid,
            "na": na,
            "revenue": float(revenue),
            "total_attendance": total_attendance,
            "unique_members": unique_members,
            "top_attending_members": top_members
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/fees/summary', methods=['GET'])
@login_required
def fees_summary():
    try:
        year = int(request.args.get('year') or datetime.now().year)
        month = int(request.args.get('month') or datetime.now().month)
    except ValueError:
        return jsonify({"error": "invalid year/month"}), 400
    paid_count = Payment.query.filter_by(year=year, month=month, status='Paid').count()
    unpaid_count = Payment.query.filter_by(year=year, month=month, status='Unpaid').count()
    total_members = paid_count + unpaid_count
    # Monthly price from settings
    try:
        monthly_price = float(get_setting('monthly_price') or '8')
    except Exception:
        monthly_price = 8.0
    # Sum of recorded monthly transactions for the given month/year (no fallback)
    tx_sum = db.session.query(func.coalesce(func.sum(PaymentTransaction.amount), 0.0)).filter(
        PaymentTransaction.plan_type == 'monthly',
        PaymentTransaction.year == year,
        PaymentTransaction.month == month
    ).scalar() or 0.0
    # Use transactions only for paid_total (no fallback)
    paid_total = float(tx_sum)
    unpaid_total = float(unpaid_count * monthly_price)
    payment_percent = float((paid_count / total_members) * 100.0) if total_members else 0.0
    return jsonify({
        'ok': True,
        'year': year,
        'month': month,
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'paid_total': round(paid_total, 2),
        'unpaid_total': round(unpaid_total, 2),
        'payment_percent': round(payment_percent, 2),
    })

@app.route('/api/fees/month', methods=['GET'])
@login_required
def fees_month_detail():
    try:
        year = int(request.args.get('year') or datetime.now().year)
        month = int(request.args.get('month') or datetime.now().month)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid year/month"}), 400
    
    payments = Payment.query.filter_by(year=year, month=month).all()
    paid_count = sum(1 for p in payments if p.status == 'Paid')
    unpaid_count = sum(1 for p in payments if p.status == 'Unpaid')
    
    try:
        currency = get_setting('currency_code') or 'PKR'
    except Exception:
        currency = 'PKR'
    
    collected = 0.0
    members_data = []
    
    for p in payments:
        member = db.session.get(Member, p.member_id)
        if not member:
            continue
        
        amount = 0.0
        paid_date = None
        
        if p.status == 'Paid':
            tx = PaymentTransaction.query.filter_by(
                member_id=member.id,
                year=year,
                month=month
            ).order_by(PaymentTransaction.created_at.desc()).first()
            if tx:
                amount = tx.amount or 0.0
                paid_date = tx.created_at.strftime('%Y-%m-%d') if tx.created_at else None
                collected += amount
        
        members_data.append({
            'member_id': member.id,
            'name': member.name,
            'phone': member.phone,
            'email': member.email,
            'admission_date': member.admission_date.strftime('%Y-%m-%d') if member.admission_date else None,
            'is_active': member.is_active,
            'status': p.status,
            'amount': amount,
            'paid_date': paid_date
        })
    
    return jsonify({
        'ok': True,
        'year': year,
        'month': month,
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'collected': round(collected, 2),
        'currency': currency,
        'members': members_data
    })

@app.route('/api/fees/unpaid-summary', methods=['GET'])
@login_required
def fees_unpaid_summary():
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    
    try:
        monthly_price = float(get_setting('monthly_price') or '8')
    except Exception:
        monthly_price = 8.0
    
    members = Member.query.all()
    unpaid_members = []
    
    for member in members:
        # Find all unpaid payments
        unpaid_payments = Payment.query.filter_by(
            member_id=member.id,
            status='Unpaid'
        ).order_by(Payment.year.desc(), Payment.month.desc()).all()
        
        if not unpaid_payments:
            continue
        
        months_unpaid = len(unpaid_payments)
        total_due = months_unpaid * monthly_price
        
        # Find last paid month
        last_paid = Payment.query.filter_by(
            member_id=member.id,
            status='Paid'
        ).order_by(Payment.year.desc(), Payment.month.desc()).first()
        
        last_paid_month = None
        if last_paid:
            month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            last_paid_month = f"{month_names[last_paid.month - 1]} {last_paid.year}"
        
        unpaid_members.append({
            'id': member.id,
            'name': member.name,
            'phone': member.phone,
            'last_paid_month': last_paid_month,
            'months_unpaid': months_unpaid,
            'total_due': round(total_due, 2)
        })
    
    return jsonify({
        'ok': True,
        'members': unpaid_members
    })

@app.route('/api/member/<int:member_id>/payment-history', methods=['GET'])
@login_required
def member_payment_history(member_id):
    member = db.session.get(Member, member_id)
    if not member:
        return jsonify({'ok': False, 'error': 'Member not found'}), 404
    
    # Get all payments for this member
    payments = Payment.query.filter_by(member_id=member_id).order_by(Payment.year, Payment.month).all()

    # Build a dict for quick lookup
    payment_map = {(p.year, p.month): p for p in payments}

    # Determine admission month/year
    admission_date = member.admission_date
    start_year = admission_date.year
    start_month = admission_date.month
    now = datetime.now()
    end_year = now.year
    end_month = now.month

    # Build a list of (year, month) from admission to now
    ym_list = []
    y, m = start_year, start_month
    while (y < end_year) or (y == end_year and m <= end_month):
        ym_list.append((y, m))
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1

    month_names = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    payment_list = []
    last_paid = None
    months_unpaid = 0
    for y, m in ym_list:
        p = payment_map.get((y, m))
        status = p.status if p else 'Unpaid'
        amount = None
        paid_date = None
        if status == 'Paid' and p:
            tx = PaymentTransaction.query.filter_by(
                member_id=member.id,
                year=y,
                month=m
            ).order_by(PaymentTransaction.created_at.desc()).first()
            if tx:
                amount = tx.amount
                paid_date = tx.created_at.strftime('%Y-%m-%d') if tx.created_at else None
            if not last_paid:
                last_paid = f"{month_names[m-1]} {y}"
        if status == 'Unpaid':
            months_unpaid += 1
        payment_list.append({
            'year': y,
            'month': m,
            'month_name': month_names[m-1],
            'status': status,
            'amount': amount,
            'paid_date': paid_date
        })

    return jsonify({
        'ok': True,
        'member': {
            'id': member.id,
            'name': member.name,
            'phone': member.phone,
            'email': member.email or '',
            # 'cnic': member.cnic,  # Removed because attribute does not exist
            # 'address': member.address,  # Removed because attribute does not exist
            # 'gender': member.gender,  # Removed because attribute does not exist
            # 'date_of_birth': member.date_of_birth.strftime('%Y-%m-%d') if member.date_of_birth else None,  # Removed
            'admission_date': member.admission_date.strftime('%Y-%m-%d') if member.admission_date else None,
            'monthly_price': float(member.monthly_fee) if member.monthly_fee else 0,
            'referred_by': member.referred_by,
            'is_active': getattr(member, 'is_active', True),
            # 'notes': member.notes  # Removed because attribute does not exist
        },
        'last_paid_month': last_paid,
        'months_unpaid': months_unpaid,
        'payments': payment_list,
        'currency': 'PKR'
    })

@app.route('/api/payment/pay-now', methods=['POST'])
@login_required
def api_payment_pay_now():
    payload = request.get_json(silent=True) or {}
    try:
        member_id = int(payload.get('member_id') or 0)
    except Exception:
        return jsonify({'ok': False, 'error': 'member_id required'}), 400
    year = int(payload.get('year') or datetime.now().year)
    
    # Handle month - could be int or string like "2025-12"
    month_val = payload.get('month')
    if month_val:
        if isinstance(month_val, str) and '-' in month_val:
            # Parse "YYYY-MM" format
            parts = month_val.split('-')
            if len(parts) == 2:
                year = int(parts[0])
                month = int(parts[1])
            else:
                month = int(month_val)
        else:
            month = int(month_val)
    else:
        month = datetime.now().month
    
    method = (payload.get('method') or 'cash').strip()

    member = db.session.get(Member, member_id)
    if not member:
        return jsonify({'ok': False, 'error': 'Member not found'}), 404

    # Derive amount
    amount = None
    if payload.get('amount') is not None:
        try:
            amount = float(payload['amount'])
        except Exception:
            amount = None
    if amount is None:
        try:
            mf = getattr(member, 'monthly_fee', None)
            amount = float(mf) if mf not in (None, '') else None
        except Exception:
            amount = None
    if amount is None:
        try:
            amount = float(get_setting('monthly_price') or 0)
        except Exception:
            amount = 0.0

    # Ensure monthly Payment exists
    p = Payment.query.filter_by(member_id=member_id, year=year, month=month).first()
    if not p:
        p = Payment(member_id=member_id, year=year, month=month, status='Unpaid')
        db.session.add(p)
        db.session.flush()

    # Create transaction
    try:
        user_id = session.get('user_id')
    except Exception:
        user_id = None
    tx = PaymentTransaction(
        member_id=member_id,
        user_id=user_id,
        plan_type=getattr(member, 'plan_type', 'monthly') or 'monthly',
        year=year,
        month=month,
        amount=amount,
        method=method,
    )
    db.session.add(tx)
    p.status = 'Paid'
    db.session.commit()

    currency = get_setting('currency_code') or 'PKR'
    return jsonify({
        'ok': True,
        'transaction_id': tx.id,
        'receipt_url': url_for('receipt_view', tx_id=tx.id),
        'amount': amount,
        'currency': currency,
    })


@app.route('/api/fees/mark-paid', methods=['POST'])
@login_required
def api_fees_mark_paid():
    """Compatibility endpoint that records payment and returns a receipt URL.
    Accepts JSON: { member_id, month, year, method?, amount? }
    """
    payload = request.get_json(silent=True) or {}
    try:
        member_id = int(payload.get('member_id') or 0)
    except Exception:
        return jsonify({'ok': False, 'error': 'member_id required'}), 400
    year = int(payload.get('year') or datetime.now().year)
    month = int(payload.get('month') or datetime.now().month)
    method = (payload.get('method') or 'cash').strip()

    member = db.session.get(Member, member_id)
    if not member:
        return jsonify({'ok': False, 'error': 'Member not found'}), 404

    # Amount resolve: explicit -> member.monthly_fee -> global monthly_price
    amount = None
    if payload.get('amount') is not None:
        try:
            amount = float(payload['amount'])
        except Exception:
            amount = None
    if amount is None:
        try:
            mf = getattr(member, 'monthly_fee', None)
            amount = float(mf) if mf not in (None, '') else None
        except Exception:
            amount = None
    if amount is None:
        try:
            amount = float(get_setting('monthly_price') or 0)
        except Exception:
            amount = 0.0

    # Ensure monthly Payment exists
    p = Payment.query.filter_by(member_id=member_id, year=year, month=month).first()
    if not p:
        p = Payment(member_id=member_id, year=year, month=month, status='Unpaid')
        db.session.add(p)
        db.session.flush()

    # Create transaction
    try:
        user_id = session.get('user_id')
    except Exception:
        user_id = None
    tx = PaymentTransaction(
        member_id=member_id,
        user_id=user_id,
        plan_type=getattr(member, 'plan_type', 'monthly') or 'monthly',
        year=year,
        month=month,
        amount=amount,
        method=method,
    )
    db.session.add(tx)
    p.status = 'Paid'
    db.session.commit()

    currency = get_setting('currency_code') or 'PKR'
    return jsonify({
        'ok': True,
        'message': 'Payment processed and receipt generated.',
        'transaction_id': tx.id,
        'receipt_url': url_for('receipt_view', tx_id=tx.id),
        'amount': amount,
        'currency': currency,
    })


def _render_receipt_context(tx: PaymentTransaction):
    member = db.session.get(Member, tx.member_id)
    gym_name = get_gym_name()
    currency = get_setting('currency_code') or 'PKR'
    month_names = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    month_name = month_names[(tx.month or 1) - 1] if tx.month else '-'
    return {
        'gym_name': gym_name,
        'currency': currency,
        'tx': tx,
        'member': member,
        'month_name': month_name,
    }


@app.route('/receipt/<int:tx_id>')
@login_required
def receipt_view(tx_id: int):
    tx = db.session.get(PaymentTransaction, tx_id)
    if not tx:
        return 'Receipt not found', 404
    ctx = _render_receipt_context(tx)
    return render_template('receipt.html', **ctx)


@app.route('/receipt/member/<int:member_id>/<int:year>/<int:month>')
@login_required
def receipt_for_period(member_id: int, year: int, month: int):
    tx = (
        PaymentTransaction.query
        .filter_by(member_id=member_id, year=year, month=month)
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )
    if not tx:
        return 'No receipt for this period', 404
    ctx = _render_receipt_context(tx)
    return render_template('receipt.html', **ctx)

def send_whatsapp_template(to_phone: str, template_name: str, lang_code: str = 'en', body_params: list[str] | None = None) -> tuple[bool, str]:
    token = os.getenv('WHATSAPP_TOKEN')
    phone_id = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
    if not token or not phone_id:
        return False, 'WhatsApp configuration missing (token/phone id)'
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    components = []
    if body_params:
        components = [{
            'type': 'body',
            'parameters': [{'type': 'text', 'text': str(v)} for v in body_params]
        }]
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'template',
        'template': {
            'name': template_name,
            'language': {'code': lang_code},
            'components': components
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
    except Exception as e:
        return False, f"request error: {e}"
    ok = 200 <= r.status_code < 300
    try:
        data = r.json()
    except Exception:
        data = {'text': r.text}
    return ok, (data if ok else f"{r.status_code}: {data}")

def send_whatsapp_text(to_phone: str, text: str) -> tuple[bool, str | dict]:
    token = os.getenv('WHATSAPP_TOKEN')
    phone_id = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
    if not token or not phone_id:
        return False, 'WhatsApp configuration missing (token/phone id)'
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = { 'Authorization': f'Bearer {token}', 'Content-Type': 'application/json' }
    payload = { 'messaging_product': 'whatsapp', 'to': to_phone, 'type': 'text', 'text': { 'preview_url': False, 'body': text } }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
    except Exception as e:
        return False, f'request error: {e}'
    try:
        js = r.json()
    except Exception:
        js = {'text': r.text}
    ok = 200 <= r.status_code < 300
    return ok, (js if ok else f"{r.status_code}: {js}")

def send_bulk_template_reminders(year: int, month: int) -> dict:
    template_name = os.getenv('WHATSAPP_TEMPLATE_FEE_REMINDER_NAME')
    lang = os.getenv('WHATSAPP_TEMPLATE_LANG', 'en')
    if not template_name:
        return {"ok": False, "error": "WHATSAPP_TEMPLATE_FEE_REMINDER_NAME not set"}
    unpaid = Payment.query.filter_by(year=year, month=month, status='Unpaid').all()
    sent, failed = 0, 0
    for p in unpaid:
        m = db.session.get(Member, p.member_id)
        if not m:
            continue
        phone = _normalize_phone(m.phone or '')
        if not phone:
            failed += 1
            continue
        month_name = datetime(year, month, 1).strftime('%B')
        body_params = [m.name, month_name, str(year)]
        ok, _ = send_whatsapp_template(phone, template_name, lang, body_params)
        if ok:
            sent += 1
        else:
            failed += 1
    return {"ok": True, "sent": sent, "failed": failed}

@app.route('/api/fees/remind/template', methods=['POST'])
@login_required
def fees_remind_template():
    try:
        year = int(request.args.get('year') or datetime.now().year)
        month = int(request.args.get('month') or datetime.now().month)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid year/month"}), 400
    result = send_bulk_template_reminders(year, month)
    status = 200 if result.get('ok') else 400
    return jsonify(result), status


# Duplicate removed: send_bulk_text_reminders
@app.route('/api/members/<int:member_id>', methods=['PUT'])
@login_required
def update_member(member_id):
    m = Member.query.get_or_404(member_id)
    data = request.json or {}
    changed = {}
    name = (data.get('name') or '').strip()
    if name:
        m.name = name; changed['name'] = name
    phone = (data.get('phone') or '').strip()
    if phone:
        m.phone = phone; changed['phone'] = phone
    admission = (data.get('admission_date') or '').strip()
    if admission:
        try:
            m.admission_date = datetime.fromisoformat(admission).date(); changed['admission_date'] = m.admission_date.isoformat()
        except Exception:
            pass
    plan_type = (data.get('plan_type') or '').lower().strip()
    if plan_type in ('monthly','yearly'):
        m.plan_type = plan_type; changed['plan_type'] = plan_type
    access_tier = (data.get('access_tier') or '').lower().strip()
    if access_tier in ('standard','unlimited'):
        m.access_tier = access_tier; changed['access_tier'] = access_tier
    email = (data.get('email') or '').strip()
    if email:
        m.email = email; changed['email'] = email
    training_type = (data.get('training_type') or '').lower().strip()
    if training_type in ('standard','personal','cardio','other'):
        if training_type == 'other':
            training_type = 'standard'
        m.training_type = training_type; changed['training_type'] = training_type
    if 'custom_training' in data:
        m.custom_training = (data.get('custom_training') or '').strip() or None; changed['custom_training'] = m.custom_training
    if 'monthly_fee' in data:
        try:
            val = data.get('monthly_fee')
            if val not in (None, ''):
                m.monthly_fee = float(val); changed['monthly_fee'] = m.monthly_fee
        except Exception:
            pass
    if 'special_tag' in data:
        m.special_tag = bool(data.get('special_tag')); changed['special_tag'] = bool(data.get('special_tag'))
    if changed:
        db.session.commit()
        append_audit('member.update', {'member_id': m.id, **changed, 'user_id': session.get('user_id')})
    return jsonify({'ok': True, 'member': m.to_dict(), 'changed': changed})


# API: upload member photo
@app.route('/api/members/<int:member_id>/photo', methods=['POST'])
@login_required
def upload_member_photo(member_id):
    _ = Member.query.get_or_404(member_id)
    if 'photo' not in request.files:
        return jsonify({"ok": False, "error": "No photo file provided (field name 'photo')"}), 400
# Duplicate removed: upload_member_photo (kept first definition above)

# API: get member payments
# Duplicate removed: get_payments (kept first definition above)

# API: update payment (mark paid/unpaid)
# Duplicate removed: update_payment (kept first definition above)

# Export member payments to excel
# Duplicate removed: update_member (kept first definition above)
@login_required
# Duplicate removed: set_member_plan (kept first definition above)
# Duplicate removed: record_payment (kept first definition above)
# Duplicate removed: message_member (kept first definition above)
# Duplicate removed: list_uploads (kept first definition at line 1200)
# Duplicate removed: get_upload (kept first definition at line 1215)
# Duplicate removed: upload_data_file (kept first definition at line 1235)
# Duplicate removed: fees_page (kept first definition at line 1303)
# Duplicate removed: fees_api (kept first definition at line 1320)

@login_required
# Duplicate removed: api_payment_pay_now

# Duplicate removed: api_fees_mark_paid

# Duplicate removed: receipt_view

# Duplicate removed: receipt_for_period
# Duplicate removed: fees_remind_template

# Duplicate removed: send_monthly_unpaid_template_job

# Common aliases to reduce 404s when typing URLs
@app.route('/index')

@app.route('/home')

# Members page (requires login) - keeps existing template
# Duplicate removed: index
@app.route('/api/members', methods=['GET'])
@login_required
def list_members():
    q = (request.args.get('search') or '').strip()
    query = Member.query
    if q:
        like = f"%{q}%"
        filters = [Member.name.ilike(like), Member.phone.ilike(like)]
        # Support searching by Serial No (e.g., 1001 or #1001)
        s = q
        if s.startswith('#'):
            s = s[1:]
        if s.isdigit():
            num = int(s)
            # If it looks like a serial (1001+), map to id = serial - 1000
            if num >= 1001:
                filters.append(Member.id == (num - 1000))
            else:
                # Also allow direct id matching
                filters.append(Member.id == num)
        query = query.filter(or_(*filters))
    members = query.order_by(Member.id.desc()).all()
    return jsonify([m.to_dict() for m in members])

# Duplicate removed: get_member (kept first definition above)


# API: delete member (remove payments and photo files)
# Duplicate removed: delete_member (kept first definition above)

# API: update member (name, phone, admission_date, plan_type, access_tier)
# Duplicate removed: update_member
# Duplicate removed: upload_member_photo
@login_required
# Duplicate removed: set_member_plan (kept first definition above)
# Duplicate removed: record_payment (kept first definition above)
# Duplicate removed: message_member (kept first definition above)
# Duplicate removed: list_uploads (kept first definition at line 1200)
# Duplicate removed: get_upload (kept first definition at line 1215)
# Duplicate removed: upload_data_file (kept first definition at line 1235)
# Duplicate removed: fees_page (kept first definition at line 1303)
# Duplicate removed: fees_api (kept first definition at line 1320)

# Duplicate removed: fees_remind_template
@app.route('/admin/schedule/run-now', methods=['POST'])
@admin_required
def schedule_run_now():
    now = datetime.now()
    res = send_bulk_template_reminders(now.year, now.month)
    status = 200 if res.get('ok') else 400
    return jsonify(res), status

# Duplicate removed: send_monthly_unpaid_template_job
@app.route('/')
def home():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# Common aliases to reduce 404s when typing URLs
def index_alias():
    # Redirect to the members page (requires login)
    return redirect(url_for('index'))

def home_alias():
    # Same behavior as root: send logged-in users to dashboard, else to login
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# Members page (requires login) - keeps existing template
# Duplicate removed: index
@app.route('/api/members', methods=['POST'])
@login_required
def add_member():
    data = request.json
    name = data.get('name')
    phone = data.get('phone')
    admission = data.get('admission_date')
    email = (data.get('email') or '').strip() or None
    training_type = (data.get('training_type') or 'standard').lower().strip()
    special_tag = bool(data.get('special_tag'))
    custom_training = (data.get('custom_training') or '').strip() or None
    monthly_fee = None
    try:
        if data.get('monthly_fee') not in (None, ''):
            monthly_fee = float(data.get('monthly_fee'))
    except Exception:
        monthly_fee = None
    if not (name and admission):
        return jsonify({"error":"name and admission_date required"}), 400
    admission_date = datetime.fromisoformat(admission).date()
    plan_type = (data.get('plan_type') or 'monthly').lower()
    if plan_type not in ('monthly','yearly'):
        plan_type = 'monthly'
    if training_type not in ('standard','personal','cardio','other'):
        training_type = 'standard'
    if training_type == 'other':
        # store as standard fallback but keep custom_training text
        training_type = 'standard'
    m = Member(name=name, phone=phone, email=email, training_type=training_type, custom_training=custom_training, monthly_fee=monthly_fee, special_tag=special_tag, admission_date=admission_date, plan_type=plan_type, referral_code=_gen_referral_code())
    db.session.add(m)
    db.session.commit()
    # initialize payment rows for the admission year
    for month in range(1,13):
        if datetime(admission_date.year, month, 1).date() < admission_date:
            status = "N/A"
        elif month == admission_date.month:
            status = "Paid"  # Admission month is automatically marked as paid
        else:
            status = "Unpaid"
        p = Payment(member_id=m.id, year=admission_date.year, month=month, status=status)
        db.session.add(p)
    db.session.commit()
    append_audit('member.create', {'member_id': m.id, 'name': m.name, 'phone': m.phone, 'admission_date': m.admission_date.isoformat(), 'plan_type': m.plan_type})
    return jsonify(m.to_dict()), 201

# API: list members
# Duplicate removed: list_members
@app.route('/api/members/<int:member_id>', methods=['DELETE'])
@login_required
def delete_member(member_id):
    m = Member.query.get_or_404(member_id)
    # delete related payments
    Payment.query.filter_by(member_id=member_id).delete()
    # delete any stored photos
    for e in ALLOWED_IMAGE_EXTS:
        pth = os.path.join(UPLOAD_FOLDER, f"member_{member_id}{e}")
        try:
            if os.path.exists(pth):
                os.remove(pth)
        except Exception:
            pass
    db.session.delete(m)
    db.session.commit()
    append_audit('member.delete', {'member_id': member_id})
    return jsonify({"ok": True})

# API: update member (name, phone, admission_date, plan_type, access_tier)
# Duplicate removed: update_member
# Duplicate removed: upload_member_photo
@app.route('/api/members/<int:member_id>/payments', methods=['GET'])
@login_required
def get_payments(member_id):
    payments = Payment.query.filter_by(member_id=member_id).order_by(Payment.year, Payment.month).all()
    return jsonify([p.to_dict() for p in payments])

# API: update payment (mark paid/unpaid)
@app.route('/api/payments/<int:payment_id>', methods=['PUT'])
@login_required
def update_payment(payment_id):
    p = Payment.query.get_or_404(payment_id)
    data = request.json
    status = data.get('status')
    if status not in ('Paid','Unpaid','N/A'):
        return jsonify({"error":"status must be Paid, Unpaid or N/A"}), 400
    p.status = status
    db.session.commit()
    append_audit('payment.update', {'payment_id': payment_id, 'status': status, 'user_id': session.get('user_id')})
    return jsonify(p.to_dict())

# Export member payments to excel
@app.route('/api/members/<int:member_id>/export', methods=['GET'])
@login_required
def export_member(member_id):
    member = Member.query.get_or_404(member_id)
    payments = Payment.query.filter_by(member_id=member_id).order_by(Payment.year, Payment.month).all()
    rows = []
    for p in payments:
        rows.append({"Year": p.year, "Month": p.month, "Status": p.status})
    df = pd.DataFrame(rows)
    out_path = os.path.join(BASE_DIR, f"member_{member_id}_payments.xlsx")
    df.to_excel(out_path, index=False)
    return send_file(out_path, as_attachment=True)

def ensure_payment_rows(member: Member, year: int):
    # Create payment rows for a year if missing
    existing = {(p.month) for p in Payment.query.filter_by(member_id=member.id, year=year).all()}
    for m in range(1, 13):
        if m in existing:
            continue
        status = 'Unpaid'
        if year == member.admission_date.year:
            first_day = datetime(year, m, 1).date()
            status = 'N/A' if first_day < member.admission_date else 'Unpaid'
        p = Payment(member_id=member.id, year=year, month=m, status=status)
        db.session.add(p)
    db.session.commit()

# Removed dangling decorators block from lines 2162-2196

@app.route('/api/fees/mark-unpaid', methods=['POST'])
@login_required
def api_fees_mark_unpaid():
    try:
        data = request.get_json() or {}
        member_id = int(data.get('member_id') or 0)
        year = int(data.get('year') or 0)
        month = int(data.get('month') or 0)
        if not member_id or not year or not month:
            return jsonify({'ok': False, 'error': 'member_id, year, month required'}), 400

        p = Payment.query.filter_by(member_id=member_id, year=year, month=month).first()
        if not p:
            # Create row if missing, default to Unpaid
            p = Payment(member_id=member_id, year=year, month=month, status='Unpaid')
            db.session.add(p)
        else:
            p.status = 'Unpaid'
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    debug_mode = os.getenv('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)
