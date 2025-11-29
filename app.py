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
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(50))
    admission_date = db.Column(db.Date, nullable=False)
    plan_type = db.Column(db.String(20), default='monthly')
    referral_code = db.Column(db.String(32), unique=True)
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
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12
    status = db.Column(db.String(20), nullable=False)  # Paid/Unpaid/N/A
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "member_id": self.member_id, "year": self.year, "month": self.month, "status": self.status}

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

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'price': float(self.price or 0.0),
            'stock': self.stock,
            'category': self.category or '',
            'sku': self.sku or '',
            'is_active': bool(self.is_active),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


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

    def to_dict(self, include_items: bool = False):
        data = {
            'id': self.id,
            'invoice_number': self.invoice_number,
            'customer_name': self.customer_name or '',
            'subtotal': float(self.subtotal or 0.0),
            'tax': float(self.tax or 0.0),
            'discount': float(self.discount or 0.0),
            'total': float(self.total or 0.0),
            'payment_method': self.payment_method or '',
            'note': self.note or '',
            'channel': self.channel,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'synced_from_offline': bool(self.synced_from_offline),
            'verification_hash': self.verification_hash,
        }
        if include_items:
            data['items'] = [item.to_dict() for item in self.items]
        return data


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    name = db.Column(db.String(140), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False, default=0.0)
    total_price = db.Column(db.Float, nullable=False, default=0.0)

    def to_dict(self):
        return {
            'id': self.id,
            'sale_id': self.sale_id,
            'product_id': self.product_id,
            'name': self.name,
            'quantity': self.quantity,
            'unit_price': float(self.unit_price or 0.0),
            'total_price': float(self.total_price or 0.0),
        }


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
                _ = send_bulk_text_reminders(now.year, now.month)
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
        uid = session.get('user_id')
        if not uid:
            return redirect(url_for('login', next=request.path))
        user = db.session.get(User, uid)
        if not user or (user.role or 'staff') != 'admin':
            flash('Admin access required', 'warning')
            return redirect(url_for('dashboard'))
        return view_func(*args, **kwargs)
    return wrapper

# Auth routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            _log_login_event(user, 'password')
            # Optional: create backup on login
            try:
                trigger_backup_on_login()
                if os.getenv('AUTO_BACKUP_ON_LOGIN', '0') not in ('0','false','False','') and 'local' in (os.getenv('AUTO_BACKUP_DEST', 'local').lower()):
                    flash('Backup created after login (see backups folder).', 'info')
            except Exception:
                pass
            next_url = request.args.get('next') or url_for('dashboard')
            return redirect(next_url)
        flash('Invalid username or password', 'danger')
    # Google OAuth status for UX
    gid = os.getenv('GOOGLE_CLIENT_ID')
    gsecret = os.getenv('GOOGLE_CLIENT_SECRET')
    google_missing = []
    if not gid:
        google_missing.append('GOOGLE_CLIENT_ID')
    if not gsecret:
        google_missing.append('GOOGLE_CLIENT_SECRET')
    google_ready = len(google_missing) == 0
    template = 'login_modern.html'
    if request.args.get('legacy') == '1':
        template = 'login.html'
    return render_template(template, gym_name=get_gym_name(), google_ready=google_ready, google_missing=google_missing, google_client_id=gid or '')


@app.route('/auth/google/verify', methods=['POST'])
def auth_google_verify():
    data = request.get_json(silent=True) or {}
    cred = data.get('credential') or ''
    client_id = os.getenv('GOOGLE_CLIENT_ID') or ''
    if not cred or not client_id:
        return jsonify({'ok': False, 'error': 'missing token or client id'}), 400
    try:
        idinfo = google_id_token.verify_oauth2_token(cred, google_requests.Request(), client_id)
        # idinfo contains 'sub', 'email', 'email_verified', 'name', 'picture' etc.
        if not idinfo.get('email_verified', False):
            return jsonify({'ok': False, 'error': 'email not verified'}), 400
        sub = idinfo.get('sub')
        email = idinfo.get('email') or ''
        name = idinfo.get('name') or email or 'user'
    except Exception as e:
        return jsonify({'ok': False, 'error': f'invalid token: {e}'}), 400

    # Reuse same user linking logic as oauth callback
    acct = OAuthAccount.query.filter_by(provider='google', provider_id=sub).first()
    if acct:
        user = db.session.get(User, acct.user_id)
    else:
        base_username = email or name.replace(' ', '').lower()
        username = base_username or f'user{sub[:6]}'
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
    try:
        trigger_backup_on_login()
    except Exception:
        pass
    return jsonify({'ok': True, 'username': user.username})

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
    try:
        trigger_backup_on_login()
    except Exception:
        pass
    next_url = request.args.get('next') or url_for('dashboard')
    return redirect(next_url)

# Simple homepage redirects to dashboard (auth required)
@app.route('/')
def home():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# Common aliases to reduce 404s when typing URLs
@app.route('/index')
def index_alias():
    # Redirect to the members page (requires login)
    return redirect(url_for('index'))

@app.route('/home')
def home_alias():
    # Same behavior as root: send logged-in users to dashboard, else to login
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

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

# API: add member
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
        status = "N/A" if datetime(admission_date.year, month, 1).date() < admission_date else "Unpaid"
        p = Payment(member_id=m.id, year=admission_date.year, month=month, status=status)
        db.session.add(p)
    db.session.commit()
    append_audit('member.create', {'member_id': m.id, 'name': m.name, 'phone': m.phone, 'admission_date': m.admission_date.isoformat(), 'plan_type': m.plan_type})
    return jsonify(m.to_dict()), 201

# API: list members
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

# API: get single member
@app.route('/api/members/<int:member_id>', methods=['GET'])
@login_required
def get_member(member_id):
    m = Member.query.get_or_404(member_id)
    return jsonify(m.to_dict())


# API: delete member (remove payments and photo files)
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
    f = request.files['photo']
    if not f or (f.filename or '').strip() == '':
        return jsonify({"ok": False, "error": "Empty file"}), 400
    ok, resp = _save_member_image(member_id, f)
    if ok:
        return jsonify({"ok": True, "image_url": resp})
    return jsonify({"ok": False, "error": resp}), 400

# API: get member payments
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

@app.route('/api/members/<int:member_id>/plan', methods=['PUT'])
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
    
    payments = Payment.query.filter_by(member_id=member_id).order_by(
        Payment.year.desc(),
        Payment.month.desc()
    ).all()
    
    # Find last paid month
    last_paid = None
    for p in payments:
        if p.status == 'Paid':
            month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            last_paid = f"{month_names[p.month - 1]} {p.year}"
            break    
    months_unpaid = sum(1 for p in payments if p.status == 'Unpaid')
    
    payment_list = []
    month_names = ['January', 'February', 'March', 'April', 'May', 'June', 
                   'July', 'August', 'September', 'October', 'November', 'December']
    
    for p in payments:
        amount = None
        paid_date = None
        
        if p.status == 'Paid':
            tx = PaymentTransaction.query.filter_by(
                member_id=member.id,
                year=p.year,
                month=p.month
            ).order_by(PaymentTransaction.created_at.desc()).first()
            if tx:
                amount = tx.amount
                paid_date = tx.created_at.strftime('%Y-%m-%d') if tx.created_at else None
        
        payment_list.append({
            'year': p.year,
            'month': p.month,
            'month_name': month_names[p.month - 1],
            'status': p.status,
            'amount': amount,
            'paid_date': paid_date
        })
    
    return jsonify({
        'ok': True,
        'member': {
            'id': member.id,
            'name': member.name,
            'phone': member.phone,
            'email': member.email,
            'cnic': member.cnic,
            'address': member.address,
            'gender': member.gender,
            'date_of_birth': member.date_of_birth.strftime('%Y-%m-%d') if member.date_of_birth else None,
            'admission_date': member.admission_date.strftime('%Y-%m-%d') if member.admission_date else None,
            'monthly_price': float(member.monthly_price) if member.monthly_price else 0,
            'referred_by': member.referred_by,
            'is_active': member.is_active,
            'notes': member.notes
        },
        'last_paid_month': last_paid,
        'months_unpaid': months_unpaid,
        'payments': payment_list,
        'currency': member.currency or 'PKR'
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
    month = int(payload.get('month') or datetime.now().month)
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

@app.route('/admin/schedule/run-now', methods=['POST'])
@admin_required
def schedule_run_now():
    now = datetime.now()
    res = send_bulk_template_reminders(now.year, now.month)
    status = 200 if res.get('ok') else 400
    return jsonify(res), status

def _smart_column_mapper(df_columns):
    """AI-powered automatic field mapping for Excel/CSV uploads"""
    column_map = {}
    
    # Enhanced column mapping patterns (case-insensitive, multi-language support)
    patterns = {
        'name': ['name', 'member name', 'full name', 'fullname', 'student name', 'customer', 'client', 
                 'naam', 'نام', 'member', 'first name', 'fname', 'last name', 'lname'],
        'phone': ['phone', 'mobile', 'contact', 'number', 'phone number', 'mobile number', 'whatsapp', 
                  'contact number', 'cell', 'telephone', 'tel', 'ph', 'رابطہ', 'موبائل'],
        'email': ['email', 'e-mail', 'mail', 'email address', 'ای میل', 'gmail', 'inbox'],
        'admission_date': ['admission', 'admission date', 'join date', 'joining date', 'date', 'start date', 
                          'reg date', 'registration date', 'registered', 'enrolled', 'enroll date', 
                          'داخلہ', 'تاریخ', 'admission_date', 'joining_date'],
        'plan_type': ['plan', 'plan type', 'subscription', 'package', 'membership', 'پلان', 
                      'plan_type', 'subscription_type'],
        'access_tier': ['access', 'tier', 'access tier', 'level', 'category', 'type', 
                        'رسائی', 'access_tier'],
        'training_type': ['training', 'training type', 'workout', 'workout type', 'exercise', 'gym type',
                         'تربیت', 'training_type', 'workout_type'],
        'special_tag': ['special', 'special tag', 'vip', 'star', 'premium', 'featured', 
                       'خاص', 'special_tag', 'vip_member'],
        'monthly_fee': ['fee', 'monthly fee', 'price', 'amount', 'monthly price', 'monthly_fee', 
                       'payment', 'cost', 'فیس', 'قیمت'],
        'cnic': ['cnic', 'id', 'national id', 'identity', 'id card', 'شناختی کارڈ'],
        'address': ['address', 'location', 'area', 'city', 'پتہ', 'مقام'],
        'gender': ['gender', 'sex', 'جنس', 'male/female'],
        'date_of_birth': ['dob', 'date of birth', 'birth date', 'birthday', 'پیدائش'],
        'referred_by': ['referred', 'referred by', 'referrer', 'reference', 'حوالہ'],
        'status': ['status', 'member status', 'active', 'is_active', 'active status', 'membership status', 
                   'حالت', 'صورتحال', 'account status'],
    }
    
    df_cols_lower = {col.lower().strip(): col for col in df_columns}
    
    # First pass: exact and partial matches
    for field, possible_names in patterns.items():
        for poss in possible_names:
            poss_lower = poss.lower()
            # Exact match
            if poss_lower in df_cols_lower:
                column_map[field] = df_cols_lower[poss_lower]
                break
            # Partial match (column contains pattern)
            for df_col_lower, original_col in df_cols_lower.items():
                if poss_lower in df_col_lower or df_col_lower in poss_lower:
                    if field not in column_map:  # Don't override exact matches
                        column_map[field] = original_col
                        break
    
    # Second pass: Fuzzy matching for close spellings
    import difflib
    for field, possible_names in patterns.items():
        if field not in column_map:
            for df_col_lower, original_col in df_cols_lower.items():
                for poss in possible_names:
                    # Check similarity ratio (>0.7 means close match)
                    if difflib.SequenceMatcher(None, poss.lower(), df_col_lower).ratio() > 0.7:
                        column_map[field] = original_col
                        break
                if field in column_map:
                    break
    
    return column_map

@app.route('/admin/members/upload', methods=['POST'])
@admin_required
def upload_members_csv():
    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    f = request.files['file']
    fname_lower = f.filename.lower()
    
    # Support CSV, Excel (.xlsx, .xls), and .xltm
    if not fname_lower.endswith(('.csv', '.xlsx', '.xls', '.xltm')):
        return jsonify({"ok": False, "error": "Supported formats: CSV, Excel (.xlsx, .xls, .xltm)"}), 400
    
    try:
        if fname_lower.endswith('.csv'):
            df = pd.read_csv(f)
        else:
            # Excel formats
            df = pd.read_excel(f, engine='openpyxl' if fname_lower.endswith(('.xlsx', '.xltm')) else None)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to parse file: {str(e)}"}), 400
    
    # Automatic column mapping
    col_map = _smart_column_mapper(df.columns.tolist())
    
    created = 0
    updated = 0
    skipped = 0
    errors = []
    
    for idx, row in df.iterrows():
        try:
            # Extract data using smart mapping
            name = str(row.get(col_map.get('name')) or '').strip() if 'name' in col_map else ''
            phone = str(row.get(col_map.get('phone')) or '').strip() if 'phone' in col_map else ''
            admission = str(row.get(col_map.get('admission_date')) or '').strip() if 'admission_date' in col_map else ''
            plan_type = str(row.get(col_map.get('plan_type')) or 'monthly').lower().strip() if 'plan_type' in col_map else 'monthly'
            access_tier = str(row.get(col_map.get('access_tier')) or 'standard').lower().strip() if 'access_tier' in col_map else 'standard'
            email = str(row.get(col_map.get('email')) or '').strip() if 'email' in col_map else ''
            training_type = str(row.get(col_map.get('training_type')) or 'standard').lower().strip() if 'training_type' in col_map else 'standard'
            special_tag_raw = str(row.get(col_map.get('special_tag')) or '').strip().lower() if 'special_tag' in col_map else ''
            special_tag = special_tag_raw in ('1','true','yes','y', 'vip', '⭐')
            
            # Extract status (is_active) from file
            status_raw = str(row.get(col_map.get('status')) or 'active').strip().lower() if 'status' in col_map else 'active'
            is_active = status_raw in ('1', 'true', 'yes', 'y', 'active', 'فعال', 'کا رہے ہیں')
            
            if not name:
                skipped += 1
                continue
            
            # Parse admission date with multiple formats
            admission_date = None
            if admission:
                for date_format in ['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d']:
                    try:
                        admission_date = datetime.strptime(admission, date_format).date()
                        break
                    except:
                        continue
                        
                if not admission_date:
                    try:
                        admission_date = datetime.fromisoformat(admission).date()
                    except:
                        pass
            
            if not admission_date:
                admission_date = datetime.now(timezone.utc).date()
            
            # Normalize enums
            if plan_type not in ('monthly','yearly'):
                plan_type = 'monthly'
            if access_tier not in ('standard','unlimited'):
                access_tier = 'standard'
            if training_type not in ('standard','personal','cardio'):
                training_type = 'standard'
            
            # Check for duplicate by phone or name
            existing = None
            if phone:
                existing = Member.query.filter_by(phone=phone).first()
            if not existing and name:
                existing = Member.query.filter_by(name=name).first()

            if existing:
                # Merge/update existing member with new details
                changed = False
                if email and existing.email != email:
                    existing.email = email
                    changed = True
                if training_type and existing.training_type != training_type:
                    existing.training_type = training_type
                    changed = True
                if existing.special_tag != special_tag:
                    existing.special_tag = special_tag
                    changed = True
                if plan_type and existing.plan_type != plan_type:
                    existing.plan_type = plan_type
                    changed = True
                if access_tier and existing.access_tier != access_tier:
                    existing.access_tier = access_tier
                    changed = True
                # Update status/is_active from file
                if hasattr(existing, 'is_active') and existing.is_active != is_active:
                    existing.is_active = is_active
                    changed = True
                # Only update admission_date if incoming is earlier (preserve earliest)
                if admission_date and (not existing.admission_date or admission_date < existing.admission_date):
                    existing.admission_date = admission_date
                    changed = True
                if changed:
                    db.session.commit()
                    updated += 1
                else:
                    skipped += 1
                # Ensure payment records exist for the admission year
                adm_year = (existing.admission_date or admission_date).year
                for mm in range(1, 12 + 1):
                    p = Payment.query.filter_by(member_id=existing.id, year=adm_year, month=mm).first()
                    if not p:
                        status = "N/A" if datetime(adm_year, mm, 1).date() < (existing.admission_date or admission_date) else "Unpaid"
                        db.session.add(Payment(member_id=existing.id, year=adm_year, month=mm, status=status))
                db.session.commit()
                continue

            # Create new member
            member_data = {
                'name': name,
                'phone': phone,
                'email': email or None,
                'training_type': training_type,
                'special_tag': special_tag,
                'admission_date': admission_date,
                'plan_type': plan_type,
                'access_tier': access_tier,
                'referral_code': _gen_referral_code()
            }
            
            # Add is_active if Member model has this field
            if hasattr(Member, 'is_active'):
                member_data['is_active'] = is_active
                
            m = Member(**member_data)
            db.session.add(m)
            db.session.commit()

            # Create payment records
            for mm in range(1, 12 + 1):
                status = "N/A" if datetime(admission_date.year, mm, 1).date() < admission_date else "Unpaid"
                db.session.add(Payment(member_id=m.id, year=admission_date.year, month=mm, status=status))
            db.session.commit()
            created += 1
            
        except Exception as e:
            errors.append(f"Row {idx + 2}: {str(e)}")
            skipped += 1
            continue
    
    # Enhanced AI detection response
    detection_quality = len(col_map) / 8.0  # Score based on how many of 8 core fields detected
    response = {
        "ok": True,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "ai_detection": {
            "columns_detected": col_map,
            "detection_quality": round(detection_quality * 100, 1),  # Percentage
            "total_columns": len(df.columns),
            "mapped_columns": len(col_map),
            "unmapped_columns": [col for col in df.columns if col not in col_map.values()],
            "confidence": "high" if detection_quality >= 0.75 else "medium" if detection_quality >= 0.5 else "low"
        }
    }
    if errors and len(errors) <= 5:
        response['errors'] = errors
    
    return jsonify(response)

# WhatsApp Cloud API helper
def _normalize_phone(phone: str) -> str:
    if not phone:
        return ''
    phone = phone.strip()
    if phone.startswith('+'):
        return phone
    # Prefer DB setting, fallback to env, default Pakistan '92'
    cc = (get_setting('whatsapp_default_country_code') or os.getenv('WHATSAPP_DEFAULT_COUNTRY_CODE') or '92')
    if cc and not phone.startswith(cc):
        if not cc.startswith('+'):
            cc = '+' + cc
        return cc + phone
    return phone

def send_whatsapp_message(to_phone: str, text: str) -> tuple[bool, str]:
    token = os.getenv('WHATSAPP_TOKEN')
    phone_id = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
    if not token or not phone_id:
        return False, 'WhatsApp configuration missing (token/phone id)'
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'text',
        'text': {'preview_url': False, 'body': text}
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

def send_bulk_text_reminders(year: int, month: int) -> dict:
    unpaid = Payment.query.filter_by(year=year, month=month, status='Unpaid').all()
    sent, failed = 0, 0
    price = (get_setting('monthly_price') or '8')
    currency = (get_setting('currency_code') or 'USD')
    gym = get_gym_name()
    default_msg = f"Hi {member.name}, your {gym} fee ({price} {currency}) for {month}/{year} may be due. Please pay if pending."
    for p in unpaid:
        member = db.session.get(Member, p.member_id)
        if not member:
            continue
        phone = _normalize_phone(member.phone or '')
        if not phone:
            failed += 1
            continue
        msg = f"Hi {member.name}, your {gym} fee ({price} {currency}) for {month}/{year} is pending. Please pay to stay active."
        ok, _ = send_whatsapp_message(phone, msg)
        if ok:
            sent += 1
        else:
            failed += 1
    return {"ok": True, "sent": sent, "failed": failed}

def _whatsapp_upload_media(filename: str, content: bytes, mime: str = 'application/pdf') -> tuple[bool, str | dict]:
    token = os.getenv('WHATSAPP_TOKEN')
    phone_id = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
    if not token or not phone_id:
        return False, 'WhatsApp configuration missing (token/phone id)'
    url = f"https://graph.facebook.com/v20.0/{phone_id}/media"
    headers = { 'Authorization': f'Bearer {token}' }
    files = { 'file': (filename, BytesIO(content), mime) }
    data = { 'messaging_product': 'whatsapp', 'type': mime }
    try:
        r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
    except Exception as e:
        return False, f"upload error: {e}"
    try:
        js = r.json()
    except Exception:
        js = {'text': r.text}
    if 200 <= r.status_code < 300 and js.get('id'):
        return True, js
    return False, f"{r.status_code}: {js}"

def send_whatsapp_document(to_phone: str, filename: str, content: bytes, caption: str = '') -> tuple[bool, str | dict]:
    ok, res = _whatsapp_upload_media(filename, content, 'application/pdf')
    if not ok:
        return False, res  # error string
    media_id = res.get('id') if isinstance(res, dict) else None
    if not media_id:
        return False, 'Failed to get media id from upload response'
    token = os.getenv('WHATSAPP_TOKEN')
    phone_id = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_phone,
        'type': 'document',
        'document': {
            'id': media_id,
            'filename': filename,
            'caption': caption or 'Membership Card'
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
    except Exception as e:
        return False, f"request error: {e}"
    ok = 200 <= r.status_code < 300
    try:
        data = r.json()
    except Exception:
        data = {'text': r.text}
    return ok, (data if ok else f"{r.status_code}: {data}")

def send_email(subject: str, body: str, to_email: str, attachments: list[tuple[str, bytes]]|None=None) -> tuple[bool, str]:
    host = os.getenv('SMTP_HOST')
    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER')
    pwd = os.getenv('SMTP_PASSWORD')
    use_tls = os.getenv('SMTP_TLS', '1') not in ('0','false','False')
    if not (host and user and pwd and to_email):
        return False, 'SMTP config missing (host/user/password or recipient)'
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = to_email
    msg.set_content(body)
    if attachments:
        for filename, content in attachments:
            msg.add_attachment(content, maintype='application', subtype='octet-stream', filename=filename)
    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo(); s.starttls(); s.ehlo(); s.login(user, pwd); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.login(user, pwd); s.send_message(msg)
        return True, 'sent'
    except Exception as e:
        return False, str(e)

def send_email_enhanced(subject: str, text_body: str, to_email: str, html_body: str|None=None, attachments: list[tuple[str, bytes]]|None=None) -> tuple[bool, str]:
    """Extended email helper supporting optional HTML alternative.

    Falls back to plain text if no HTML provided. Uses same SMTP env vars.
    """
    host = os.getenv('SMTP_HOST')
    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER')
    pwd = os.getenv('SMTP_PASSWORD')
    use_tls = os.getenv('SMTP_TLS', '1') not in ('0','false','False')
    if not (host and user and pwd and to_email):
        return False, 'SMTP config missing (host/user/password or recipient)'
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = to_email
    msg.set_content(text_body or '')
    if html_body:
        msg.add_alternative(html_body, subtype='html')
    if attachments:
        for filename, content in attachments:
            msg.add_attachment(content, maintype='application', subtype='octet-stream', filename=filename)
    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo(); s.starttls(); s.ehlo(); s.login(user, pwd); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.login(user, pwd); s.send_message(msg)
        return True, 'sent'
    except Exception as e:
        return False, str(e)

@app.route('/admin/backup/email', methods=['POST'])
@admin_required
def email_backup():
    # Build an in-memory zip with database and quick CSV exports
    mem = BytesIO()
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as z:
        # include sqlite DB
        try:
            with open(db_path, 'rb') as f:
                z.writestr('gym.db', f.read())
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Failed reading DB: {e}'}), 500
        # members.csv
        members = Member.query.all()
        rows = ['id,name,phone,admission_date']
        for m in members:
            rows.append(f"{m.id}," + f"{(m.name or '').replace(',', ' ')}," + f"{(m.phone or '').replace(',', ' ')}," + f"{m.admission_date.isoformat()}")
        z.writestr('members.csv', '\n'.join(rows))
        # payments.csv
        pays = Payment.query.order_by(Payment.member_id, Payment.year, Payment.month).all()
        rows = ['id,member_id,year,month,status,created_at']
        for p in pays:
            rows.append(f"{p.id},{p.member_id},{p.year},{p.month},{p.status},{p.created_at.isoformat()}")
        z.writestr('payments.csv', '\n'.join(rows))
        # payment_transactions.csv
        txns = PaymentTransaction.query.order_by(PaymentTransaction.created_at).all()
        rows = ['id,member_id,user_id,plan_type,year,month,amount,method,created_at']
        for t in txns:
            rows.append(f"{t.id},{t.member_id},{t.user_id},{t.plan_type},{t.year},{t.month or ''},{t.amount or ''},{t.method or ''},{t.created_at.isoformat()}")
        z.writestr('payment_transactions.csv', '\n'.join(rows))
        # audit_logs.csv (tamper-evident chain)
        logs = AuditLog.query.order_by(AuditLog.id).all()
        rows = ['id,created_at,action,data_json,prev_hash,hash']
        for r in logs:
            rows.append(f"{r.id},{r.created_at.isoformat()},{r.action},{r.data_json.replace(',', ';')},{r.prev_hash or ''},{r.hash}")
        z.writestr('audit_logs.csv', '\n'.join(rows))
    mem.seek(0)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    to_email = os.getenv('BACKUP_TO_EMAIL') or request.args.get('to')
    subject = f'Gym Backup {ts}'
    body = 'Attached is the backup (DB and CSV exports).'
    ok, resp = send_email(subject, body, to_email, attachments=[(f'backup_{ts}.zip', mem.read())])
    if ok:
        return jsonify({'ok': True, 'message': 'Backup sent'})
    return jsonify({'ok': False, 'error': resp}), 502


def _build_backup_zip_bytes() -> tuple[bool, bytes, str]:
    mem = BytesIO()
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as z:
        try:
            with open(db_path, 'rb') as f:
                z.writestr('gym.db', f.read())
        except Exception as e:
            return False, b'', f'Failed reading DB: {e}'
        members = Member.query.all()
        rows = ['id,name,phone,admission_date']
        for m in members:
            rows.append(f"{m.id}," + f"{(m.name or '').replace(',', ' ')}," + f"{(m.phone or '').replace(',', ' ')}," + f"{m.admission_date.isoformat()}")
        z.writestr('members.csv', '\n'.join(rows))
        pays = Payment.query.order_by(Payment.member_id, Payment.year, Payment.month).all()
        rows = ['id,member_id,year,month,status,created_at']
        for p in pays:
            rows.append(f"{p.id},{p.member_id},{p.year},{p.month},{p.status},{p.created_at.isoformat()}")
        z.writestr('payments.csv', '\n'.join(rows))
        txns = PaymentTransaction.query.order_by(PaymentTransaction.created_at).all()
        rows = ['id,member_id,user_id,plan_type,year,month,amount,method,created_at']
        for t in txns:
            rows.append(f"{t.id},{t.member_id},{t.user_id},{t.plan_type},{t.year},{t.month or ''},{t.amount or ''},{t.method or ''},{t.created_at.isoformat()}")
        z.writestr('payment_transactions.csv', '\n'.join(rows))
        logs = AuditLog.query.order_by(AuditLog.id).all()
        rows = ['id,created_at,action,data_json,prev_hash,hash']
        for r in logs:
            rows.append(f"{r.id},{r.created_at.isoformat()},{r.action},{r.data_json.replace(',', ';')},{r.prev_hash or ''},{r.hash}")
        z.writestr('audit_logs.csv', '\n'.join(rows))
    mem.seek(0)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return True, mem.read(), ts


def _save_backup_local(content: bytes, ts: str) -> tuple[bool, str]:
    try:
        fname = f"backup_{ts}.zip"
        path = os.path.join(BACKUP_DIR, fname)
        with open(path, 'wb') as f:
            f.write(content)
        return True, path
    except Exception as e:
        return False, str(e)


def trigger_backup_on_login() -> None:
    """Perform backup right after a successful login based on env config.

    Env controls:
      - AUTO_BACKUP_ON_LOGIN: enable when set to '1'/'true'
      - AUTO_BACKUP_DEST: comma-separated 'local', 'email', 'drive' (default: 'local')
    """
    if os.getenv('AUTO_BACKUP_ON_LOGIN', '0') in ('0', 'false', 'False', ''):
        return
    ok, data, ts = _build_backup_zip_bytes()
    if not ok:
        append_audit('backup.auto_login.error', {'error': data})
        return
    dests = (os.getenv('AUTO_BACKUP_DEST', 'local') or 'local').lower().split(',')
    results = {}
    if 'local' in dests:
        l_ok, l_path = _save_backup_local(data, ts)
        results['local'] = {'ok': l_ok, 'path': l_path}
    if 'email' in dests:
        to_email = os.getenv('BACKUP_TO_EMAIL')
        if to_email:
            subject = f'Gym Backup {ts}'
            body = 'Attached is the automatic login-time backup.'
            e_ok, e_resp = send_email(subject, body, to_email, attachments=[(f'backup_{ts}.zip', data)])
            results['email'] = {'ok': e_ok, 'response': e_resp}
        else:
            results['email'] = {'ok': False, 'error': 'BACKUP_TO_EMAIL not set'}
    if 'drive' in dests:
        d_ok, d_info = _upload_backup_to_gdrive(data, f'backup_{ts}.zip')
        results['drive'] = {'ok': d_ok, 'info': d_info}
    append_audit('backup.auto_login', {'results': results})


def perform_automatic_backup() -> dict:
    """Perform automatic backup and return results."""
    ok, data, ts = _build_backup_zip_bytes()
    if not ok:
        return {'ok': False, 'error': data}
    
    # Always save locally
    l_ok, l_path = _save_backup_local(data, ts)
    
    # Clean old backups (keep last 30)
    cleanup_old_backups(keep_count=30)
    
    return {
        'ok': l_ok,
        'timestamp': ts,
        'path': l_path,
        'size': len(data)
    }


def cleanup_old_backups(keep_count: int = 30) -> None:
    """Remove old backups, keeping only the most recent ones."""
    try:
        backups = []
        for fname in os.listdir(BACKUP_DIR):
            if fname.startswith('backup_') and fname.endswith('.zip'):
                fpath = os.path.join(BACKUP_DIR, fname)
                backups.append((os.path.getmtime(fpath), fpath))
        
        # Sort by modification time (oldest first)
        backups.sort()
        
        # Remove old backups
        if len(backups) > keep_count:
            for _, fpath in backups[:-keep_count]:
                try:
                    os.remove(fpath)
                except Exception:
                    pass
    except Exception:
        pass


def _upload_backup_to_gdrive(content: bytes, filename: str, mime: str = 'application/zip') -> tuple[bool, dict | str]:
    if not HAVE_GDRIVE:
        return False, 'Google Drive libraries not installed'
    sa_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')
    folder_id = os.getenv('DRIVE_FOLDER_ID')
    if not (sa_file and os.path.exists(sa_file)):
        return False, 'GOOGLE_SERVICE_ACCOUNT_FILE not set or file missing'
    if not folder_id:
        return False, 'DRIVE_FOLDER_ID not set'
    try:
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=['https://www.googleapis.com/auth/drive.file'])
        drive = build('drive', 'v3', credentials=creds)
        media = MediaIoBaseUpload(BytesIO(content), mimetype=mime, resumable=False)
        metadata = {'name': filename, 'parents': [folder_id]}
        file = drive.files().create(body=metadata, media_body=media, fields='id,webViewLink,webContentLink').execute()
        return True, file
    except Exception as e:
        return False, str(e)


@app.route('/admin/backup/drive', methods=['POST'])
@admin_required
def drive_backup():
    ok, data, ts = _build_backup_zip_bytes()
    if not ok:
        return jsonify({'ok': False, 'error': data}), 500
    fname = f'backup_{ts}.zip'
    ok, info = _upload_backup_to_gdrive(data, fname)
    if ok:
        return jsonify({'ok': True, 'file': info})
    return jsonify({'ok': False, 'error': info}), 502

@app.route('/admin/backup/download', methods=['GET'])
@admin_required
def download_backup():
    ok, data, ts = _build_backup_zip_bytes()
    if not ok:
        return jsonify({'ok': False, 'error': data}), 500
    return send_file(
        BytesIO(data),
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"backup_{ts}.zip",
    )


@app.route('/admin/backup/create', methods=['POST'])
@admin_required
def create_backup():
    """Manually trigger a backup."""
    result = perform_automatic_backup()
    if result['ok']:
        append_audit('backup.manual', {'timestamp': result['timestamp']})
        return jsonify(result)
    return jsonify(result), 500


@app.route('/admin/backup/list', methods=['GET'])
@admin_required
def list_backups():
    """List all available backups."""
    try:
        backups = []
        for fname in os.listdir(BACKUP_DIR):
            if fname.startswith('backup_') and fname.endswith('.zip'):
                fpath = os.path.join(BACKUP_DIR, fname)
                stat = os.stat(fpath)
                backups.append({
                    'filename': fname,
                    'size': stat.st_size,
                    'created': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'timestamp': stat.st_mtime
                })
        
        # Sort by timestamp (newest first)
        backups.sort(key=lambda x: x['timestamp'], reverse=True)
        
        return jsonify({
            'ok': True,
            'backups': backups,
            'total': len(backups),
            'total_size': sum(b['size'] for b in backups)
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/backup/restore/<filename>', methods=['POST'])
@admin_required
def restore_backup(filename):
    """Restore from a specific backup file."""
    try:
        fpath = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(fpath) or not filename.startswith('backup_'):
            return jsonify({'ok': False, 'error': 'Invalid backup file'}), 404
        
        # Extract and restore database
        with zipfile.ZipFile(fpath, 'r') as z:
            if 'gym.db' in z.namelist():
                # Backup current DB first
                current_backup = db_path + '.before_restore'
                if os.path.exists(db_path):
                    import shutil
                    shutil.copy2(db_path, current_backup)
                
                # Extract and replace
                z.extract('gym.db', BASE_DIR)
                
                append_audit('backup.restored', {
                    'filename': filename,
                    'restored_at': datetime.now().isoformat()
                })
                
                return jsonify({
                    'ok': True,
                    'message': 'Backup restored successfully',
                    'note': 'Please restart the application to apply changes'
                })
            else:
                return jsonify({'ok': False, 'error': 'Invalid backup format'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/backup/delete/<filename>', methods=['DELETE'])
@admin_required
def delete_backup(filename):
    """Delete a specific backup file."""
    try:
        fpath = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(fpath) or not filename.startswith('backup_'):
            return jsonify({'ok': False, 'error': 'Invalid backup file'}), 404
        
        os.remove(fpath)
        append_audit('backup.deleted', {'filename': filename})
        
        return jsonify({'ok': True, 'message': f'Backup {filename} deleted'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/admin/backup/download/<filename>', methods=['GET'])
@admin_required
def download_specific_backup(filename):
    """Download a specific backup file."""
    try:
        fpath = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(fpath) or not filename.startswith('backup_'):
            return jsonify({'ok': False, 'error': 'Invalid backup file'}), 404
        
        return send_file(
            fpath,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/members/<int:member_id>/remind', methods=['POST'])
@login_required
def remind_member(member_id):
    member = Member.query.get_or_404(member_id)
    phone = _normalize_phone(member.phone or '')
    if not phone:
        return jsonify({'ok': False, 'error': 'Member has no phone number'}), 400
    now = datetime.now()
    price = (get_setting('monthly_price') or '8')
    currency = (get_setting('currency_code') or 'USD')
    gym = get_gym_name()
    default_msg = f"Hi {member.name}, your {gym} fee ({price} {currency}) for {now.month}/{now.year} may be due. Please pay if pending."
    data = request.get_json(silent=True) or {}
    message = data.get('message') or default_msg
    ok, resp = send_whatsapp_message(phone, message)
    if ok:
        return jsonify({'ok': True, 'response': resp})
    return jsonify({'ok': False, 'error': resp}), 502


@app.route('/admin/whatsapp/test', methods=['POST'])
@admin_required
def whatsapp_test():
    data = request.get_json(silent=True) or {}
    to = _normalize_phone((data.get('to') or '').strip())
    msg = (data.get('message') or 'Test message from Gym Tracker').strip()
    if not to:
        return jsonify({'ok': False, 'error': 'Provide a valid phone number in international format'}), 400
    ok, resp = send_whatsapp_message(to, msg)
    if ok:
        return jsonify({'ok': True, 'response': resp})
    return jsonify({'ok': False, 'error': resp}), 502


@app.route('/admin/whatsapp/status', methods=['GET'])
def whatsapp_status():
    token_present = bool(os.getenv('WHATSAPP_TOKEN'))
    phone_id_present = bool(os.getenv('WHATSAPP_PHONE_NUMBER_ID'))
    cc = (get_setting('whatsapp_default_country_code') or os.getenv('WHATSAPP_DEFAULT_COUNTRY_CODE') or '92')
    missing = []
    if not token_present:
        missing.append('WHATSAPP_TOKEN')
    if not phone_id_present:
        missing.append('WHATSAPP_PHONE_NUMBER_ID')
    return jsonify({
        'ok': token_present and phone_id_present,
        'missing': missing,
        'default_country_code': cc
    })


@app.route('/api/admin/login-logs', methods=['GET'])
@admin_required
def admin_login_logs():
    logs = [
        {
            'id': log.id,
            'username': log.username,
            'method': log.method,
            'ip_address': log.ip_address,
            'created_at': log.created_at.isoformat(),
        }
        for log in LoginLog.query.order_by(LoginLog.created_at.desc()).limit(100).all()
    ]
    return jsonify(logs)


@app.route('/api/admin/staff', methods=['POST'])
@admin_required
def admin_add_staff():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = request.form.get('password') or ''
    role = (data.get('role') or 'staff').strip().lower()
    if not (username and password):
        return jsonify({'ok': False, 'error': 'username and password required'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'ok': False, 'error': 'username already exists'}), 400
    if role not in ('staff', 'admin'):
        role = 'staff'
    user = User(username=username, password_hash=generate_password_hash(password), role=role)
    db.session.add(user)
    db.session.commit()
    append_audit('admin.staff.create', {'user_id': user.id, 'role': role, 'created_by': session.get('user_id')})
    return jsonify({'ok': True, 'user': {'id': user.id, 'username': user.username, 'role': user.role}})


@app.route('/api/admin/permissions', methods=['GET', 'POST'])
@admin_required
def admin_permissions():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        set_setting_json('staff_permissions', data.get('permissions') or [])
        append_audit('admin.permissions.update', {'user_id': session.get('user_id')})
        return jsonify({'ok': True})
    return jsonify({'permissions': get_setting_json('staff_permissions', []) or []})

@app.route('/admin/settings/gym-name', methods=['GET', 'POST'])
@admin_required
def gym_name_setting():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        name = (data.get('gym_name') or '').strip()
        if len(name) < 2:
            return jsonify({'ok': False, 'error': 'gym_name too short'}), 400
        set_setting('gym_name', name)
        append_audit('setting.update.gym_name', {'user_id': session.get('user_id'), 'gym_name': name})
        return jsonify({'ok': True, 'gym_name': name})
    return jsonify({'ok': True, 'gym_name': get_gym_name()})

# Public onboarding endpoints (enabled only until onboarding_done=="1")
def _onboarding_needed() -> bool:
    return (get_setting('onboarding_done', '0') != '1')  # type: ignore[name-defined]

@app.route('/onboarding/status', methods=['GET'])
def onboarding_status():
    needed = _onboarding_needed()
    settings = {
        'gym_name': get_gym_name(),
        'gym_purpose': get_setting('gym_purpose', '') if not needed else '',  # type: ignore[name-defined]
        'currency_code': get_setting('currency_code', 'USD') if not needed else 'USD',
        'monthly_price': get_setting('monthly_price', '8') if not needed else '8',
        'features': get_setting_json('features', []) if not needed else [],
        'logo': get_setting('logo_filename', '') if not needed else ''
    }
    return jsonify({'ok': True, 'needed': needed, 'settings': settings})

@app.route('/onboarding/complete', methods=['POST'])
def onboarding_complete():
    if not _onboarding_needed():
        return jsonify({'ok': False, 'error': 'Onboarding already completed'}), 403
    data = request.get_json(silent=True) or {}
    gym_name = (data.get('gym_name') or '').strip() or get_gym_name()
    gym_purpose = (data.get('gym_purpose') or '').strip()
    currency_code = (data.get('currency_code') or 'USD').upper()
    monthly_price = str(data.get('monthly_price') or '8')
    features = data.get('features') or []
    skip = bool(data.get('skip'))
    if not skip:
        set_setting('gym_name', gym_name)  # type: ignore[name-defined]
        set_setting('gym_purpose', gym_purpose)  # type: ignore[name-defined]
        set_setting('currency_code', currency_code)  # type: ignore[name-defined]
        set_setting('monthly_price', monthly_price)  # type: ignore[name-defined]
        set_setting_json('features', features)
    set_setting('onboarding_done', '1')  # type: ignore[name-defined]
    append_audit('onboarding.complete', {'skip': skip, 'currency_code': currency_code, 'monthly_price': monthly_price})
    return jsonify({'ok': True})

@app.route('/onboarding/logo', methods=['POST'])
def onboarding_logo():
    if not _onboarding_needed():
        return jsonify({'ok': False, 'error': 'Onboarding already completed'}), 403
    if 'logo' not in request.files:
        return jsonify({'ok': False, 'error': "Missing 'logo' file"}), 400
    f = request.files['logo']
    if not f or (f.filename or '').strip() == '':
        return jsonify({'ok': False, 'error': 'Empty file'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        return jsonify({'ok': False, 'error': 'Only PNG/JPG/WEBP'}), 400
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    dest_rel = f"/static/uploads/logo{ext}"
    dest_abs = os.path.join(UPLOAD_FOLDER, f"logo{ext}")
    for e in ('.png', '.jpg', '.jpeg', '.webp'):
        pth = os.path.join(UPLOAD_FOLDER, f"logo{e}")
        try:
            if os.path.exists(pth):
                os.remove(pth)
        except Exception:
            pass
    f.save(dest_abs)
    set_setting('logo_filename', dest_rel)  # type: ignore[name-defined]
    append_audit('onboarding.logo', {'path': dest_rel})
    return jsonify({'ok': True, 'logo': dest_rel})

@app.route('/admin/logo', methods=['POST'])
@admin_required
def admin_logo():
    if 'logo' not in request.files:
        return jsonify({'ok': False, 'error': "Missing 'logo' file"}), 400
    f = request.files['logo']
    if not f or (f.filename or '').strip() == '':
        return jsonify({'ok': False, 'error': 'Empty file'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        return jsonify({'ok': False, 'error': 'Only PNG/JPG/WEBP'}), 400
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    dest_rel = f"/static/uploads/logo{ext}"
    dest_abs = os.path.join(UPLOAD_FOLDER, f"logo{ext}")
    for e in ('.png', '.jpg', '.jpeg', '.webp'):
        pth = os.path.join(UPLOAD_FOLDER, f"logo{e}")
        try:
            if os.path.exists(pth):
                os.remove(pth)
        except Exception:
            pass
    f.save(dest_abs)
    set_setting('logo_filename', dest_rel)  # type: ignore[name-defined]
    append_audit('settings.logo', {'path': dest_rel, 'user_id': session.get('user_id')})
    return jsonify({'ok': True, 'logo': dest_rel})

@app.route('/admin/settings/general', methods=['POST'])
@admin_required
def admin_settings_general():
    data = request.get_json(silent=True) or {}
    if 'gym_name' in data:
        set_setting('gym_name', (data.get('gym_name') or '').strip())  # type: ignore[name-defined]
    if 'gym_purpose' in data:
        set_setting('gym_purpose', (data.get('gym_purpose') or '').strip())  # type: ignore[name-defined]
    if 'currency_code' in data:
        set_setting('currency_code', (data.get('currency_code') or 'USD').upper())  # type: ignore[name-defined]
    if 'monthly_price' in data:
        set_setting('monthly_price', str(data.get('monthly_price') or '8'))  # type: ignore[name-defined]
    if 'features' in data:
        set_setting_json('features', data.get('features') or [])
    if 'whatsapp_default_country_code' in data:
        code = str(data.get('whatsapp_default_country_code') or '').lstrip('+')
        set_setting('whatsapp_default_country_code', code or '92')  # type: ignore[name-defined]
    append_audit('settings.general', {'user_id': session.get('user_id')})
    return jsonify({'ok': True})

@app.route('/admin/settings/reminders', methods=['GET', 'POST'])
@admin_required
def admin_settings_reminders():
    if request.method == 'GET':
        enabled = get_setting('reminder_enabled') or '0'
        hour = get_setting('reminder_hour') or '9'
        minute = get_setting('reminder_minute') or '0'
        return jsonify({
            'ok': True,
            'enabled': enabled in ('1', 'true', 'True'),
            'hour': int(hour),
            'minute': int(minute)
        })
    data = request.get_json(silent=True) or {}
    if 'enabled' in data:
        set_setting('reminder_enabled', '1' if data.get('enabled') else '0')
    if 'hour' in data:
        set_setting('reminder_hour', str(int(data.get('hour', 9))))
    if 'minute' in data:
        set_setting('reminder_minute', str(int(data.get('minute', 0))))
    append_audit('settings.reminders', {'user_id': session.get('user_id')})
    return jsonify({'ok': True, 'message': 'Reminder settings saved. Restart server to apply schedule.'})

@app.route('/admin/audit/verify', methods=['GET'])
@admin_required
def audit_verify():
    prev_hash = None
    ok = True
    broken_at = None
    for rec in AuditLog.query.order_by(AuditLog.id.asc()).all():
        digest = _audit_hash(prev_hash, rec.created_at.isoformat(), rec.action, rec.data_json)
        if digest != rec.hash or rec.prev_hash != prev_hash:
            ok = False
            broken_at = rec.id
            break
        prev_hash = rec.hash
    return jsonify({'ok': ok, 'broken_at': broken_at})

@app.route('/r/<code>', methods=['GET', 'POST'])
def referral_register(code):
    ref = Member.query.filter_by(referral_code=code).first()
    if not ref:
        return render_template('referral_register.html', error='Invalid referral link', gym_name=get_gym_name())
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        admission = (request.form.get('admission_date') or '').strip()
        plan_type = (request.form.get('plan_type') or 'monthly').lower()
        if not (name and admission):
            return render_template('referral_register.html', error='Name and admission date required', referrer=ref, gym_name=get_gym_name())
        try:
            admission_date = datetime.fromisoformat(admission).date()
        except Exception:
            return render_template('referral_register.html', error='Invalid date', referrer=ref, gym_name=get_gym_name())
        if plan_type not in ('monthly','yearly'):
            plan_type = 'monthly'
        m = Member(name=name, phone=phone, admission_date=admission_date, plan_type=plan_type, referral_code=_gen_referral_code(), referred_by=ref.id, access_tier='unlimited')
        db.session.add(m)
        db.session.commit()
        for mm in range(1, 13):
            status = "N/A" if datetime(admission_date.year, mm, 1).date() < admission_date else "Unpaid"
            db.session.add(Payment(member_id=m.id, year=admission_date.year, month=mm, status=status))
        db.session.commit()
        append_audit('member.create.referral', {'member_id': m.id, 'referred_by': ref.id})
        return render_template('referral_register.html', success=True, referrer=ref, gym_name=get_gym_name())
    return render_template('referral_register.html', referrer=ref, gym_name=get_gym_name())

# Member card: download as PDF
@app.route('/api/members/<int:member_id>/card', methods=['GET'])
@login_required
def member_card_download(member_id):
    m = Member.query.get_or_404(member_id)
    ok, pdf_bytes, fname = _build_member_card_pdf_bytes(m)
    if not ok:
        return jsonify({'ok': False, 'error': fname or 'Failed to build PDF'}), 500
    return send_file(BytesIO(pdf_bytes), mimetype='application/pdf', as_attachment=True, download_name=fname)

# Member card: send via email and/or WhatsApp
@app.route('/api/members/<int:member_id>/card/send', methods=['POST'])
@login_required
def member_card_send(member_id):
    m = Member.query.get_or_404(member_id)
    payload = request.get_json(silent=True) or {}
    email_to = (payload.get('email') or '').strip()
    whatsapp_to = _normalize_phone((payload.get('whatsapp') or '').strip())
    if not email_to and not whatsapp_to:
        return jsonify({'ok': False, 'error': 'Provide email and/or whatsapp'}), 400
    ok, pdf_bytes, fname = _build_member_card_pdf_bytes(m)
    if not ok:
        return jsonify({'ok': False, 'error': fname or 'Failed to build PDF'}), 500
    results = {}
    # Email
    if email_to:
        subj = f"Membership Card - {m.name} (#" + str(1000 + (m.id or 0)) + ")"
        body = f"Attached is the membership card for {m.name}."
        e_ok, e_resp = send_email(subj, body, email_to, attachments=[(fname, pdf_bytes)])
        results['email'] = {'ok': e_ok, 'response': e_resp}
    # WhatsApp
    if whatsapp_to:
        caption = f"{m.name} - ZAIDAN FITNESS CARD"
        w_ok, w_resp = send_whatsapp_document(whatsapp_to, fname, pdf_bytes, caption)
        results['whatsapp'] = {'ok': w_ok, 'response': w_resp}
    overall_ok = all(v.get('ok') for v in results.values()) if results else False
    status = 200 if overall_ok else 207  # multi-status style
    return jsonify({'ok': overall_ok, 'results': results}), status

# API: list payment transactions for a member
@app.route('/api/members/<int:member_id>/transactions', methods=['GET'])
@login_required
def member_transactions(member_id):
    _ = Member.query.get_or_404(member_id)
    txns = PaymentTransaction.query.filter_by(member_id=member_id).order_by(PaymentTransaction.created_at.desc()).all()
    out = []
    for t in txns:
        out.append({
            'id': t.id,
            'member_id': t.member_id,
            'user_id': t.user_id,
            'plan_type': t.plan_type,
            'year': t.year,
            'month': t.month,
            'amount': t.amount,
            'method': t.method,
            'created_at': t.created_at.isoformat(),
        })
    return jsonify(out)

# ADMIN: Complete System Reset
@app.route('/admin/system/reset', methods=['POST'])
@admin_required
def system_reset():
    """Complete system reset: clears all data, resets to factory defaults.
    
    WARNING: This will delete:
    - All members and their payment records
    - All users (except default admin)
    - All payment transactions
    - All audit logs
    - All products and sales (POS data)
    - All uploaded files and member photos
    - All settings (reset to defaults)
    
    Requires admin authentication.
    """
    data = request.get_json(silent=True) or {}
    confirm = (data.get('confirm') or '').strip().upper()
    
    if confirm != 'RESET':
        return jsonify({
            'ok': False, 
            'error': 'Confirmation required. Send {"confirm": "RESET"} to proceed.'
        }), 400
    
    try:
        # 1. Delete all database records
        db.session.query(SaleItem).delete()
        db.session.query(Sale).delete()
        db.session.query(Product).delete()
        db.session.query(PaymentTransaction).delete()
        db.session.query(Payment).delete()
        db.session.query(Member).delete()
        db.session.query(AuditLog).delete()
        db.session.query(UploadedFile).delete()
        db.session.query(LoginLog).delete()
        db.session.query(OAuthAccount).delete()
        db.session.query(Setting).delete()
        
        # 2. Delete all users except recreate admin
        db.session.query(User).delete()
        
        # 3. Commit deletions
        db.session.commit()
        
        # 4. Recreate default admin user
        admin_username = os.getenv('ADMIN_USERNAME', 'admin')
        admin_password = os.getenv('ADMIN_PASSWORD', 'admin123')
        admin_user = User(
            username=admin_username, 
            password_hash=generate_password_hash(admin_password), 
            role='admin'
        )
        db.session.add(admin_user)
        
        # 5. Reset core settings to defaults
        set_setting('gym_name', 'ZAIDAN FITNESS RECORD')
        set_setting('currency_code', 'USD')
        set_setting('monthly_price', '8')
        set_setting('whatsapp_default_country_code', '92')
        set_setting('onboarding_done', '0')
        
        db.session.commit()
        
        # 6. Delete all uploaded files from filesystem
        try:
            for filename in os.listdir(UPLOAD_FOLDER):
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                if os.path.isfile(filepath):
                    os.remove(filepath)
        except Exception:
            pass
        
        # 7. Delete all backup files
        try:
            for filename in os.listdir(BACKUP_DIR):
                filepath = os.path.join(BACKUP_DIR, filename)
                if os.path.isfile(filepath):
                    os.remove(filepath)
        except Exception:
            pass
        
        # 8. Delete all data uploads
        try:
            data_uploads_dir = os.path.join(BASE_DIR, 'data_uploads')
            if os.path.exists(data_uploads_dir):
                for filename in os.listdir(data_uploads_dir):
                    filepath = os.path.join(data_uploads_dir, filename)
                    if os.path.isfile(filepath):
                        os.remove(filepath)
        except Exception:
            pass
        
        # 9. Log out current session
        session.clear()
        
        # 10. Log the reset event (new audit chain starts)
        append_audit('system.reset', {
            'performed_by': 'admin',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'note': 'Complete system reset - all data cleared'
        })
        
        return jsonify({
            'ok': True,
            'message': 'System reset completed successfully',
            'details': {
                'database': 'All tables cleared',
                'users': f'Reset to default admin ({admin_username})',
                'files': 'All uploads deleted',
                'settings': 'Reset to defaults',
                'session': 'Logged out'
            },
            'next_steps': [
                'Login with default credentials',
                'Complete onboarding setup',
                'Add new members and configure settings'
            ]
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'ok': False,
            'error': f'Reset failed: {str(e)}'
        }), 500

@app.route('/dashboard/excel')
@login_required
def excel_dashboard():
    """Excel-based auto-updating dashboard"""
    return render_template('excel_dashboard.html', gym_name=get_gym_name())

@app.route('/api/excel/data')
@login_required
def excel_data_endpoint():
    """API endpoint for Excel data with auto-update support"""
    EXCEL_FILE_PATH = os.path.join(BASE_DIR, 'data_file.xlsx')
    
    if not os.path.exists(EXCEL_FILE_PATH):
        # Return sample data if file doesn't exist
        return jsonify([{'Category': 'No Data', 'Value': 0}])
    
    try:
        df = pd.read_excel(EXCEL_FILE_PATH)
        # Convert to JSON format
        data = df.to_dict(orient='records')
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Initialize automatic backup scheduler
def init_backup_scheduler():
    """Initialize automatic backup scheduler if enabled."""
    if not HAVE_APSCHEDULER:
        return
    
    # Check if automatic backups are enabled
    auto_backup_enabled = os.getenv('AUTO_BACKUP_ENABLED', '1') in ('1', 'true', 'True')
    if not auto_backup_enabled:
        return
    
    # Get backup interval (default: every 6 hours)
    backup_interval = int(os.getenv('BACKUP_INTERVAL_HOURS', '6'))
    
    def backup_job():
        """Wrapper function to run backup within app context"""
        with app.app_context():
            perform_automatic_backup()
    
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            func=backup_job,
            trigger='interval',
            hours=backup_interval,
            id='automatic_backup',
            name='Automatic Gym Backup',
            replace_existing=True
        )
        scheduler.start()
        
        # Perform initial backup on startup (within app context)
        with app.app_context():
            result = perform_automatic_backup()
            if result.get('ok'):
                print(f"✓ Initial backup created: {result.get('path')}")
        
        print(f"✓ Automatic backup scheduler started (every {backup_interval} hours)")
        print(f"✓ Backups saved to: {BACKUP_DIR}")
    except Exception as e:
        print(f"Warning: Could not start backup scheduler: {e}")


if __name__ == '__main__':
    # Initialize backup scheduler
    init_backup_scheduler()
    
    print("="*60)
    print("GYM MANAGEMENT SYSTEM - STARTING")
    print("="*60)
    print(f"Database: {db_path}")
    print(f"Backups: {BACKUP_DIR}")
    print(f"Server: http://0.0.0.0:5000")
    print("="*60)
    
    app.run(host='0.0.0.0', port=5000, debug=True)
