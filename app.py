from datetime import datetime, timedelta, date
from io import BytesIO
import os
import time
import secrets
import sqlite3
from pathlib import Path

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, session, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_, cast, String

from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import arabic_reshaper
from bidi.algorithm import get_display


# -------------------- App / Config --------------------
app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "app.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SUPPORT_UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "support_uploads")
app.config["AD_UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "ad_uploads")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
os.makedirs(app.config["SUPPORT_UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["AD_UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------- Models --------------------
class User(db.Model, UserMixin):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    company_name = db.Column(db.String(200), nullable=True)
    owner_name = db.Column(db.String(200), nullable=True)
    phone = db.Column(db.String(80), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    address = db.Column(db.String(300), nullable=True)

    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_support = db.Column(db.Boolean, default=False, nullable=False)
    is_advertiser = db.Column(db.Boolean, default=False, nullable=False)
    advertiser_type = db.Column(db.String(40), nullable=True)  # MERCHANT / SHIPPING_COMPANY / CUSTOMS_CLEARER

    # Service provider accounts
    is_clearer = db.Column(db.Boolean, default=False, nullable=False)
    is_carrier = db.Column(db.Boolean, default=False, nullable=False)
    map_url = db.Column(db.String(500), nullable=True)

    # License
    license_key = db.Column(db.String(64), unique=True, nullable=True)
    license_activated = db.Column(db.Boolean, default=False, nullable=False)

    # Subscription plan
    plan_type = db.Column(db.String(30), default="TRIAL", nullable=False)  # TRIAL/MONTHLY/YEARLY/TWO_YEARS
    starts_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)

    # لا يصير فعال إلا بعد تفعيل اللايسنس
    is_active = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    def remaining_days(self) -> int:
        if not self.expires_at:
            return 0
        delta = self.expires_at - datetime.utcnow()
        return max(0, delta.days)

    def status_text(self) -> str:
        if not self.is_active:
            return "غير فعال"
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return "غير فعال"
        return "فعال"

    def ensure_valid(self):
        # لازم license مفعل
        if not self.license_activated:
            self.is_active = False
            return

        # انتهاء الاشتراك
        if self.expires_at and datetime.utcnow() > self.expires_at:
            self.is_active = False


def generate_license_key():
    return secrets.token_hex(32)


def plan_to_expiry(plan_type: str):
    now = datetime.utcnow()
    if plan_type == "TRIAL":
        return now + timedelta(days=3)
    if plan_type == "MONTHLY":
        return now + timedelta(days=30)
    if plan_type == "YEARLY":
        return now + timedelta(days=365)
    if plan_type == "TWO_YEARS":
        return now + timedelta(days=730)
    return now + timedelta(days=3)


class Factory(db.Model):
    __tablename__ = "factory"
    id = db.Column(db.Integer, primary_key=True)
    # بدون unique حتى ما يتعارض بين المستخدمين
    name = db.Column(db.String(200), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)


class Country(db.Model):
    __tablename__ = "country"
    id = db.Column(db.Integer, primary_key=True)
    # بدون unique حتى ما يتعارض بين المستخدمين
    name = db.Column(db.String(200), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)


class Shipment(db.Model):
    __tablename__ = "shipment"

    id = db.Column(db.Integer, primary_key=True)

    factory_id = db.Column(db.Integer, db.ForeignKey("factory.id"), nullable=False)
    country_id = db.Column(db.Integer, db.ForeignKey("country.id"), nullable=False)

    invoice_no = db.Column(db.String(100), nullable=True)
    invoice_date = db.Column(db.Date, nullable=True)

    invoice_amount = db.Column(db.Float, nullable=True)
    invoice_currency = db.Column(db.String(10), nullable=False, default="USD")  # USD / EUR

    bol_no = db.Column(db.String(120), nullable=True)

    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    clearer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    carrier_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    clearance_company_name = db.Column(db.String(220), nullable=True)
    clearance_cost = db.Column(db.Float, nullable=True)
    clearance_status = db.Column(db.String(30), default="PENDING", nullable=False)  # ACCEPTED / CLEARING / DONE / REJECTED / PENDING
    clearance_quote_status = db.Column(db.String(30), default="REQUESTED", nullable=False)  # REQUESTED / PROPOSED / ACCEPTED / REJECTED
    carrier_quote_status = db.Column(db.String(30), default="REQUESTED", nullable=False)  # REQUESTED / PROPOSED / ACCEPTED / REJECTED

    # tracking field (manual link or number)
    msc_tracking = db.Column(db.String(300), nullable=True)

    shipping_mode = db.Column(db.String(20), nullable=False, default="BRI")  # BRI / BAHRI
    cargo_type = db.Column(db.String(20), nullable=False, default="CONTAINER")  # CAR / CONTAINER

    containers_count = db.Column(db.Integer, nullable=True, default=0)

    ship_date = db.Column(db.Date, nullable=True)
    arrival_date = db.Column(db.Date, nullable=True)

    shipping_cost = db.Column(db.Float, nullable=True)
    shipping_paid_by = db.Column(db.String(200), nullable=True)

    other_details = db.Column(db.Text, nullable=True)
    shipping_details = db.Column(db.Text, nullable=True)

    status = db.Column(db.String(20), nullable=False, default="NEW")  # NEW / IN_TRANSIT / RECEIVED
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    factory = db.relationship("Factory")
    country = db.relationship("Country")
    owner = db.relationship("User", foreign_keys=[owner_id])
    clearer = db.relationship("User", foreign_keys=[clearer_id])
    carrier = db.relationship("User", foreign_keys=[carrier_id])


class ReportSetting(db.Model):
    __tablename__ = "report_setting"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False, index=True)
    fields_csv = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = db.relationship("User")

    def fields_list(self):
        return [x.strip() for x in (self.fields_csv or "").split(",") if x.strip()]


class SystemSetting(db.Model):
    __tablename__ = "system_setting"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)



class SupportTicket(db.Model):
    __tablename__ = "support_ticket"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    subject = db.Column(db.String(220), nullable=False)
    status = db.Column(db.String(30), default="OPEN", nullable=False, index=True)  # OPEN / CLOSED
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    messages = db.relationship("TicketMessage", back_populates="ticket", order_by="TicketMessage.created_at.asc()")


class TicketMessage(db.Model):
    __tablename__ = "ticket_message"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("support_ticket.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=True)
    attachment_path = db.Column(db.String(300), nullable=True)
    attachment_name = db.Column(db.String(220), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    ticket = db.relationship("SupportTicket", back_populates="messages")
    sender = db.relationship("User")


class Advertisement(db.Model):
    __tablename__ = "advertisement"

    id = db.Column(db.Integer, primary_key=True)
    advertiser_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    title = db.Column(db.String(220), nullable=False)
    image_path = db.Column(db.String(300), nullable=False)
    link_url = db.Column(db.String(500), nullable=True)

    amount = db.Column(db.Float, nullable=True)
    currency = db.Column(db.String(10), default="IQD", nullable=False)  # IQD / USD
    duration_days = db.Column(db.Integer, default=7, nullable=False)
    starts_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(30), default="ACTIVE", nullable=False, index=True)  # ACTIVE / ARCHIVED
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    advertiser = db.relationship("User", foreign_keys=[advertiser_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])

    def is_live(self) -> bool:
        now = datetime.utcnow()
        return self.status == "ACTIVE" and self.starts_at <= now <= self.ends_at

    def remaining_days(self) -> int:
        delta = self.ends_at - datetime.utcnow()
        return max(0, delta.days)




class ServiceRating(db.Model):
    __tablename__ = "service_rating"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipment.id"), nullable=False, index=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    service_type = db.Column(db.String(30), nullable=False, index=True)  # CLEARER / CARRIER
    stars = db.Column(db.Integer, nullable=False, default=5)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    shipment = db.relationship("Shipment", foreign_keys=[shipment_id])
    provider = db.relationship("User", foreign_keys=[provider_id])
    user = db.relationship("User", foreign_keys=[user_id])


class ProviderFavorite(db.Model):
    __tablename__ = "provider_favorite"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    service_type = db.Column(db.String(30), nullable=False, index=True)  # CLEARER / CARRIER
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
    provider = db.relationship("User", foreign_keys=[provider_id])


class SystemNotification(db.Model):
    __tablename__ = "system_notification"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(220), nullable=False)
    body = db.Column(db.Text, nullable=True)
    url = db.Column(db.String(500), nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")

# -------------------- Helpers --------------------
def parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def status_badge(status: str):
    mapping = {
        "NEW": ("طلبية جديدة", "danger"),
        "IN_TRANSIT": ("قيد الشحن", "warning"),
        "RECEIVED": ("مستلمة", "success"),
    }
    return mapping.get(status, ("غير معروف", "secondary"))


def money(v):
    if v is None:
        return ""
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v)


def ar(txt: str) -> str:
    if not txt:
        return ""
    reshaped = arabic_reshaper.reshape(txt)
    return get_display(reshaped)


def safe_str(v, limit=None):
    s = "" if v is None else str(v)
    if limit and len(s) > limit:
        return s[:limit] + "…"
    return s


ADVERTISER_TYPES = {
    "MERCHANT": "تاجر",
    "SHIPPING_COMPANY": "شركة شحن",
    "CUSTOMS_CLEARER": "مخلص",
}
AD_CURRENCIES = {"IQD": "دينار عراقي", "USD": "دولار"}
CLEARANCE_STATUSES = {"PENDING": "بالانتظار", "ACCEPTED": "مقبولة", "CLEARING": "جاري التخليص", "DONE": "منجزة", "REJECTED": "مرفوضة"}
QUOTE_STATUSES = {"REQUESTED": "بانتظار السعر", "PROPOSED": "بانتظار الموافقة", "ACCEPTED": "مقبول", "REJECTED": "مرفوض"}
SERVICE_TYPES = {"CLEARER": "مخلص", "CARRIER": "شركة نقل"}


def advertiser_type_label(value):
    return ADVERTISER_TYPES.get(value or "", "-")


def clearance_status_label(value):
    return CLEARANCE_STATUSES.get(value or "PENDING", "بالانتظار")


def quote_status_label(value):
    return QUOTE_STATUSES.get(value or "REQUESTED", "بانتظار السعر")


def create_notification(user_id, title, body="", url=None):
    if not user_id:
        return
    db.session.add(SystemNotification(user_id=user_id, title=title, body=body, url=url, is_read=False))


def notification_count():
    if not current_user.is_authenticated or is_admin():
        return 0
    return SystemNotification.query.filter_by(user_id=current_user.id, is_read=False).count()


def latest_notifications(limit=5):
    if not current_user.is_authenticated or is_admin():
        return []
    return SystemNotification.query.filter_by(user_id=current_user.id).order_by(SystemNotification.id.desc()).limit(limit).all()


def is_provider_favorite(provider_id, service_type, user_id=None):
    if not current_user.is_authenticated and user_id is None:
        return False
    uid = user_id or current_user.id
    return ProviderFavorite.query.filter_by(user_id=uid, provider_id=provider_id, service_type=service_type).first() is not None


def is_clearer():
    return current_user.is_authenticated and bool(getattr(current_user, "is_clearer", False))


def is_carrier():
    return current_user.is_authenticated and bool(getattr(current_user, "is_carrier", False))


def provider_rating(provider_id, service_type=None):
    q = ServiceRating.query.filter_by(provider_id=provider_id)
    if service_type:
        q = q.filter_by(service_type=service_type)
    avg = q.with_entities(func.avg(ServiceRating.stars)).scalar() or 0
    count = q.with_entities(func.count(ServiceRating.id)).scalar() or 0
    return {"avg": round(float(avg), 1) if avg else 0, "count": count}


def provider_stars(avg):
    try:
        avg = float(avg or 0)
    except Exception:
        avg = 0
    full = int(round(avg))
    return "★" * full + "☆" * (5 - full)


def allowed_ad_file(filename: str) -> bool:
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext in {"jpg", "jpeg", "png", "gif", "webp"}


def save_ad_image(file_storage, old_path=None):
    if not file_storage or not file_storage.filename:
        return old_path
    if not allowed_ad_file(file_storage.filename):
        raise ValueError("يسمح برفع صور الإعلانات فقط: JPG, PNG, GIF, WEBP")
    original = secure_filename(file_storage.filename)
    ext = original.rsplit(".", 1)[-1].lower()
    filename = f"ad_{int(time.time())}_{secrets.token_hex(8)}.{ext}"
    full_path = os.path.join(app.config["AD_UPLOAD_FOLDER"], filename)
    file_storage.save(full_path)
    return f"ad_uploads/{filename}"


def archive_expired_ads():
    try:
        now = datetime.utcnow()
        expired = Advertisement.query.filter(Advertisement.status == "ACTIVE", Advertisement.ends_at < now).all()
        if expired:
            for ad in expired:
                ad.status = "ARCHIVED"
            db.session.commit()
    except Exception:
        db.session.rollback()


def active_ads_for_current_user(limit=12):
    if not current_user.is_authenticated or is_admin() or is_support() or getattr(current_user, "is_advertiser", False):
        return []
    archive_expired_ads()
    now = datetime.utcnow()
    return (
        Advertisement.query
        .filter(
            Advertisement.status == "ACTIVE",
            Advertisement.starts_at <= now,
            Advertisement.ends_at >= now,
            or_(Advertisement.target_user_id.is_(None), Advertisement.target_user_id == current_user.id),
        )
        .order_by(Advertisement.created_at.desc())
        .limit(limit)
        .all()
    )


REPORT_FIELDS = [
    {"key": "factory", "label": "المعمل"},
    {"key": "invoice_no", "label": "رقم الفاتورة"},
    {"key": "invoice_amount", "label": "مبلغ الفاتورة"},
    {"key": "invoice_currency", "label": "العملة"},
    {"key": "bol_no", "label": "رقم البوليصة"},
    {"key": "containers_count", "label": "عدد الحاويات"},
    {"key": "shipping_cost", "label": "مبلغ الشحن"},
    {"key": "arrival_date", "label": "تاريخ الوصول"},
    {"key": "shipping_mode", "label": "نوع الشحن"},
    {"key": "status", "label": "الحالة"},
]
REPORT_FIELD_KEYS = [f["key"] for f in REPORT_FIELDS]
REPORT_FIELD_LABELS = {f["key"]: f["label"] for f in REPORT_FIELDS}
DEFAULT_REPORT_FIELDS = REPORT_FIELD_KEYS[:]


def clean_report_fields(fields):
    cleaned = []
    for field in fields or []:
        if field in REPORT_FIELD_KEYS and field not in cleaned:
            cleaned.append(field)
    return cleaned


def get_user_report_fields(user_id=None):
    if user_id is None:
        user_id = current_user.id if current_user.is_authenticated else None
    if not user_id:
        return DEFAULT_REPORT_FIELDS[:]
    setting = ReportSetting.query.filter_by(user_id=user_id).first()
    if setting:
        selected = clean_report_fields(setting.fields_list())
        if selected:
            return selected
    return DEFAULT_REPORT_FIELDS[:]


def shipment_report_value(shipment, field_key):
    if field_key == "factory":
        return shipment.factory.name if shipment.factory else "-"
    if field_key == "invoice_no":
        return shipment.invoice_no or "-"
    if field_key == "invoice_amount":
        return money(shipment.invoice_amount) or "-"
    if field_key == "invoice_currency":
        return shipment.invoice_currency or "-"
    if field_key == "bol_no":
        return shipment.bol_no or "-"
    if field_key == "containers_count":
        return shipment.containers_count if shipment.containers_count is not None else 0
    if field_key == "shipping_cost":
        return money(shipment.shipping_cost) or "-"
    if field_key == "arrival_date":
        return shipment.arrival_date.strftime("%Y-%m-%d") if shipment.arrival_date else "-"
    if field_key == "shipping_mode":
        return "بري" if shipment.shipping_mode == "BRI" else "بحري"
    if field_key == "status":
        return status_badge(shipment.status)[0]
    return "-"


def is_admin():
    return current_user.is_authenticated and bool(getattr(current_user, "is_admin", False))


def is_support():
    return current_user.is_authenticated and bool(getattr(current_user, "is_support", False))


def is_advertiser():
    return current_user.is_authenticated and bool(getattr(current_user, "is_advertiser", False))


def allowed_support_file(filename: str) -> bool:
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext in {"jpg", "jpeg", "png", "gif", "webp"}


def save_support_attachment(file_storage):
    if not file_storage or not file_storage.filename:
        return None, None
    if not allowed_support_file(file_storage.filename):
        raise ValueError("يسمح برفع الصور فقط: JPG, PNG, GIF, WEBP")
    original = secure_filename(file_storage.filename)
    ext = original.rsplit(".", 1)[-1].lower()
    filename = f"ticket_{int(time.time())}_{secrets.token_hex(8)}.{ext}"
    full_path = os.path.join(app.config["SUPPORT_UPLOAD_FOLDER"], filename)
    file_storage.save(full_path)
    return f"support_uploads/{filename}", file_storage.filename


def owner_filter(model_query):
    """
    فلترة مركزية للبيانات حسب صاحب الحساب.
    المستخدم العادي لا يرى إلا السجلات المرتبطة بـ owner_id الخاص به.
    مدير النظام مفصول عن واجهات الشحنات أصلاً، لكن تركنا السماح هنا للحالات الإدارية الداخلية فقط.
    """
    if is_admin():
        return model_query
    return model_query.filter_by(owner_id=current_user.id)




def arrival_window_bounds(window_key: str):
    """
    يرجع حدود فلترة موعد الوصول حسب الشرط المطلوب.
    - 3days: من اليوم إلى 3 أيام قادمة
    - week: من اليوم إلى نهاية 7 أيام قادمة
    - month: من اليوم إلى نهاية الشهر الحالي
    """
    today = date.today()
    if window_key == "3days":
        return today, today + timedelta(days=3)
    if window_key == "week":
        return today, today + timedelta(days=7)
    if window_key == "month":
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        return today, next_month - timedelta(days=1)
    return None, None


def upcoming_arrivals_query(window_key: str):
    start_date, end_date = arrival_window_bounds(window_key)
    q = owner_filter(Shipment.query).filter(
        Shipment.arrival_date.isnot(None),
        Shipment.arrival_date >= start_date,
        Shipment.arrival_date <= end_date,
        Shipment.status != "RECEIVED",
    )
    return q


def get_upcoming_arrival_stats():
    if not current_user.is_authenticated or is_admin():
        return {"three_days": 0, "week": 0, "month": 0}
    return {
        "three_days": upcoming_arrivals_query("3days").with_entities(func.count(Shipment.id)).scalar() or 0,
        "week": upcoming_arrivals_query("week").with_entities(func.count(Shipment.id)).scalar() or 0,
        "month": upcoming_arrivals_query("month").with_entities(func.count(Shipment.id)).scalar() or 0,
    }


def get_upcoming_arrival_notifications(limit=8):
    if not current_user.is_authenticated or is_admin():
        return []
    today = date.today()
    month_start, month_end = arrival_window_bounds("month")
    shipments = (
        owner_filter(Shipment.query)
        .filter(
            Shipment.arrival_date.isnot(None),
            Shipment.arrival_date >= today,
            Shipment.arrival_date <= month_end,
            Shipment.status != "RECEIVED",
        )
        .order_by(Shipment.arrival_date.asc(), Shipment.id.asc())
        .limit(limit)
        .all()
    )

    notifications = []
    for shipment in shipments:
        days_left = (shipment.arrival_date - today).days
        if days_left <= 3:
            level = "urgent"
            window = "3days"
            label = "خلال 3 أيام"
        elif days_left <= 7:
            level = "week"
            window = "week"
            label = "هذا الأسبوع"
        else:
            level = "month"
            window = "month"
            label = "هذا الشهر"

        notifications.append({
            "id": shipment.id,
            "factory": shipment.factory.name if shipment.factory else "-",
            "country": shipment.country.name if shipment.country else "-",
            "arrival_date": shipment.arrival_date.strftime("%Y-%m-%d"),
            "days_left": days_left,
            "level": level,
            "window": window,
            "label": label,
            "url": url_for("shipments_list", arrival_filter=window),
        })
    return notifications


@app.context_processor
def inject_arrival_notifications():
    return {
        "arrival_stats": get_upcoming_arrival_stats(),
        "arrival_notifications": get_upcoming_arrival_notifications(),
    }

def current_owner_id():
    """يرجع رقم صاحب البيانات الحالي عند إنشاء سجلات جديدة."""
    return current_user.id if current_user.is_authenticated and not is_admin() else None


def get_owned_or_404(model, row_id):
    """
    يجلب سجلاً بعد تطبيق فلترة المالك.
    يمنع أي مستخدم من الوصول لسجل مستخدم آخر حتى لو غيّر ID من الرابط أو الفورم.
    """
    return owner_filter(model.query).filter(model.id == int(row_id)).first_or_404()


@app.context_processor
def inject_helpers():
    return {
        "status_badge": status_badge,
        "money": money,
        "is_admin_user": is_admin,
        "is_support_user": is_support,
        "is_advertiser_user": is_advertiser,
        "is_clearer_user": is_clearer,
        "is_carrier_user": is_carrier,
        "report_value": shipment_report_value,
        "report_label": REPORT_FIELD_LABELS.get,
        "advertiser_type_label": advertiser_type_label,
        "active_ads": active_ads_for_current_user,
        "clearance_status_label": clearance_status_label,
        "provider_rating": provider_rating,
        "provider_stars": provider_stars,
        "quote_status_label": quote_status_label,
        "notification_count": notification_count,
        "latest_notifications": latest_notifications,
        "is_provider_favorite": is_provider_favorite,
    }


# SeaRates integration has been retired. No API credentials are kept in code.
# Manual routing data lives in the independent Tracking Engine SQLite database.


def manual_tracking_for_shipment(shipment_id):
    """Read operator-entered route data only; never write to the engine DB here."""
    db_path = Path(os.environ.get(
        "PORTEXA_TRACKING_DB",
        "/home/alialameeri/Portexa-Tracking-Engine/data/tracking.sqlite3"
    )).expanduser()
    if not db_path.is_file():
        return None
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT loading_port,current_port,next_port,discharge_port,"
                "transshipment_port,vessel_name,voyage_number,container_number,"
                "etd,eta,current_status,operator_notes,updated_at,source "
                "FROM portexa_manual_routes WHERE source_shipment_id=?",
                (shipment_id,),
            ).fetchone()
            return dict(row) if row else None
    except (sqlite3.Error, OSError):
        app.logger.warning("Manual route unavailable for shipment %s", shipment_id)
        return None


def container_tracking_for_shipment(shipment_id):
    """Read independent per-container movements from the PORTEXA source database.

    This uses the application's own database connection and never writes to it.
    Older installations without the table simply return an empty list.
    """
    try:
        connection = db.session.connection()
        table = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ops_container_movements'"
        ).first()
        if not table:
            return []
        columns = (
            "container_number", "loading_port", "current_port", "next_port",
            "discharge_port", "transshipment_port", "vessel_name",
            "voyage_number", "etd", "eta", "current_status", "operator_notes"
        )
        rows = connection.exec_driver_sql(
            "SELECT " + ",".join(columns) +
            " FROM ops_container_movements WHERE shipment_id=? ORDER BY id",
            (int(shipment_id),)
        ).mappings().all()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        app.logger.warning("Container movements unavailable for shipment %s: %s", shipment_id, exc)
        return []


# -------------------- Auth / Guards --------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.before_request
def guard_everything():
    """
    فصل مسارات النظام حسب نوع المستخدم:
    - مدير النظام: يدخل إلى لوحة الأدمن فقط.
    - المستخدم العادي: يدخل إلى نظام الشحنات الحالي فقط.
    """
    allowed_endpoints = {"login", "logout", "static"}
    if request.endpoint in allowed_endpoints:
        return

    if not current_user.is_authenticated:
        return redirect(url_for("login"))

    current_user.ensure_valid()
    db.session.commit()

    if not current_user.is_active:
        flash("الحساب غير مفعل أو انتهى الاشتراك. تواصل مع الإدارة.", "danger")
        return redirect(url_for("logout"))

    if is_admin():
        # مدير النظام يدخل لوحة الإدارة فقط.
        if not request.path.startswith("/admin"):
            return redirect(url_for("admin_users"))
    elif is_support():
        # موظف الدعم الفني يدخل منظومة التكتات والإشعارات فقط.
        if not (request.path.startswith("/support") or request.path.startswith("/notifications")):
            return redirect(url_for("support_tickets"))
    elif is_advertiser():
        # حساب المعلن يدخل فقط إلى لوحة إعلاناته.
        if not request.path.startswith("/advertiser"):
            return redirect(url_for("advertiser_ads"))
    elif is_clearer():
        if not (request.path.startswith("/clearer") or request.path.startswith("/notifications")):
            return redirect(url_for("clearer_dashboard"))
    elif is_carrier():
        if not (request.path.startswith("/carrier") or request.path.startswith("/notifications")):
            return redirect(url_for("carrier_dashboard"))
    else:
        # المستخدم العادي ممنوع من لوحة الإدارة ولوحة الموظفين ولوحة المعلنين.
        if request.path.startswith("/admin") or request.path.startswith("/support") or request.path.startswith("/advertiser") or request.path.startswith("/clearer") or request.path.startswith("/carrier"):
            flash("غير مصرح بالدخول إلى هذه الصفحة.", "danger")
            return redirect(url_for("dashboard"))


# -------------------- DB Init + Migration --------------------
with app.app_context():
    db.create_all()

    # Retired SeaRates settings are no longer initialized.
    # Existing credentials in the database must be purged separately
    # after backing up the database (see deployment instructions).

    # Migration: add columns if old db
    try:
        insp = db.inspect(db.engine)

        def add_col_if_missing(table, col_name, sql):
            cols = [c["name"] for c in insp.get_columns(table)]
            if col_name not in cols:
                db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {sql}"))
                db.session.commit()

        if "factory" in insp.get_table_names():
            add_col_if_missing("factory", "owner_id", "owner_id INTEGER")
        if "country" in insp.get_table_names():
            add_col_if_missing("country", "owner_id", "owner_id INTEGER")
        if "shipment" in insp.get_table_names():
            add_col_if_missing("shipment", "owner_id", "owner_id INTEGER")
            add_col_if_missing("shipment", "msc_tracking", "msc_tracking VARCHAR(300)")
            add_col_if_missing("shipment", "clearer_id", "clearer_id INTEGER")
            add_col_if_missing("shipment", "carrier_id", "carrier_id INTEGER")
            add_col_if_missing("shipment", "clearance_company_name", "clearance_company_name VARCHAR(220)")
            add_col_if_missing("shipment", "clearance_cost", "clearance_cost FLOAT")
            add_col_if_missing("shipment", "clearance_status", "clearance_status VARCHAR(30) DEFAULT 'PENDING'")
            add_col_if_missing("shipment", "clearance_quote_status", "clearance_quote_status VARCHAR(30) DEFAULT 'REQUESTED'")
            add_col_if_missing("shipment", "carrier_quote_status", "carrier_quote_status VARCHAR(30) DEFAULT 'REQUESTED'")

        if "user" in insp.get_table_names():
            add_col_if_missing("user", "license_key", "license_key VARCHAR(64)")
            add_col_if_missing("user", "license_activated", "license_activated BOOLEAN DEFAULT 0")
            add_col_if_missing("user", "plan_type", "plan_type VARCHAR(30) DEFAULT 'TRIAL'")
            add_col_if_missing("user", "starts_at", "starts_at DATETIME")
            add_col_if_missing("user", "expires_at", "expires_at DATETIME")
            add_col_if_missing("user", "is_active", "is_active BOOLEAN DEFAULT 0")
            add_col_if_missing("user", "is_admin", "is_admin BOOLEAN DEFAULT 0")
            add_col_if_missing("user", "is_support", "is_support BOOLEAN DEFAULT 0")
            add_col_if_missing("user", "is_advertiser", "is_advertiser BOOLEAN DEFAULT 0")
            add_col_if_missing("user", "advertiser_type", "advertiser_type VARCHAR(40)")
            add_col_if_missing("user", "is_clearer", "is_clearer BOOLEAN DEFAULT 0")
            add_col_if_missing("user", "is_carrier", "is_carrier BOOLEAN DEFAULT 0")
            add_col_if_missing("user", "map_url", "map_url VARCHAR(500)")
            add_col_if_missing("user", "created_at", "created_at DATETIME")
            add_col_if_missing("user", "company_name", "company_name VARCHAR(200)")
            add_col_if_missing("user", "owner_name", "owner_name VARCHAR(200)")
            add_col_if_missing("user", "phone", "phone VARCHAR(80)")
            add_col_if_missing("user", "email", "email VARCHAR(200)")
            add_col_if_missing("user", "address", "address VARCHAR(300)")
    except Exception:
        pass

    # Default accounts split:
    # - admin/admin123 remains a normal system user for the shipment screens.
    # - systemadmin/admin123 is the general system administrator for user management only.

    normal_admin = User.query.filter_by(username="admin").first()
    if not normal_admin:
        normal_admin = User(username="admin", is_admin=False)
        normal_admin.set_password("admin123")
        db.session.add(normal_admin)
    else:
        # Important migration: old projects used admin/admin123 as the normal user.
        # If a previous update promoted it to system admin, bring it back to normal user.
        normal_admin.is_admin = False
        normal_admin.is_support = False
        normal_admin.is_advertiser = False
        normal_admin.is_clearer = False
        normal_admin.is_carrier = False

    normal_admin.license_key = normal_admin.license_key or generate_license_key()
    normal_admin.license_activated = True
    normal_admin.plan_type = normal_admin.plan_type or "YEARLY"
    normal_admin.starts_at = normal_admin.starts_at or datetime.utcnow()
    normal_admin.expires_at = normal_admin.expires_at or plan_to_expiry("YEARLY")
    normal_admin.is_active = True

    system_admin = User.query.filter_by(username="systemadmin").first()
    if not system_admin:
        system_admin = User(username="systemadmin", is_admin=True)
        system_admin.set_password("admin123")
        db.session.add(system_admin)
    else:
        system_admin.is_admin = True
        system_admin.is_support = False
        system_admin.is_advertiser = False
        system_admin.is_clearer = False
        system_admin.is_carrier = False

    system_admin.license_key = system_admin.license_key or generate_license_key()
    system_admin.license_activated = True
    system_admin.plan_type = "YEARLY"
    system_admin.starts_at = system_admin.starts_at or datetime.utcnow()
    system_admin.expires_at = system_admin.expires_at or plan_to_expiry("YEARLY")
    system_admin.is_active = True

    # تأمين فصل بيانات المستخدمين للنسخ القديمة:
    # أي شحنات/معامل/بلدان قديمة بدون owner_id كانت تابعة للحساب القديم admin/admin123،
    # لذلك ننقلها لهذا المستخدم العادي حتى لا تظهر لأي مستخدم جديد.
    db.session.flush()
    if normal_admin.id:
        try:
            db.session.execute(db.text("UPDATE factory SET owner_id = :uid WHERE owner_id IS NULL"), {"uid": normal_admin.id})
            db.session.execute(db.text("UPDATE country SET owner_id = :uid WHERE owner_id IS NULL"), {"uid": normal_admin.id})
            db.session.execute(db.text("UPDATE shipment SET owner_id = :uid WHERE owner_id IS NULL"), {"uid": normal_admin.id})
        except Exception:
            db.session.rollback()

    db.session.commit()


# -------------------- Auth Routes --------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("بيانات الدخول غير صحيحة.", "danger")
            return render_template("login.html")

        user.ensure_valid()
        db.session.commit()

        if not user.license_activated:
            flash("الحساب غير مفعل. يرجى تفعيل اللايسنس من الإدارة.", "danger")
            return render_template("login.html")

        if not user.is_active:
            flash("الحساب غير فعال أو انتهى الاشتراك.", "danger")
            return render_template("login.html")

        login_user(user)
        if user.is_admin:
            return redirect(url_for("admin_users"))
        if user.is_support:
            return redirect(url_for("support_tickets"))
        if getattr(user, "is_advertiser", False):
            return redirect(url_for("advertiser_ads"))
        if getattr(user, "is_clearer", False):
            return redirect(url_for("clearer_dashboard"))
        if getattr(user, "is_carrier", False):
            return redirect(url_for("carrier_dashboard"))
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.get("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


# -------------------- Dashboard --------------------
@app.get("/")
@login_required
def dashboard():
    q = owner_filter(Shipment.query)

    total_orders = q.with_entities(func.count(Shipment.id)).scalar() or 0
    total_cars = q.filter(Shipment.cargo_type == "CAR").with_entities(func.count(Shipment.id)).scalar() or 0
    total_containers = q.filter(Shipment.cargo_type == "CONTAINER").with_entities(func.count(Shipment.id)).scalar() or 0

    containers_received = q.filter(Shipment.cargo_type == "CONTAINER", Shipment.status == "RECEIVED").with_entities(func.count(Shipment.id)).scalar() or 0
    containers_not_received = q.filter(Shipment.cargo_type == "CONTAINER", Shipment.status == "NEW").with_entities(func.count(Shipment.id)).scalar() or 0
    containers_in_transit = q.filter(Shipment.cargo_type == "CONTAINER", Shipment.status == "IN_TRANSIT").with_entities(func.count(Shipment.id)).scalar() or 0

    cars_received = q.filter(Shipment.cargo_type == "CAR", Shipment.status == "RECEIVED").with_entities(func.count(Shipment.id)).scalar() or 0
    cars_not_received = q.filter(Shipment.cargo_type == "CAR", Shipment.status == "NEW").with_entities(func.count(Shipment.id)).scalar() or 0
    cars_in_transit = q.filter(Shipment.cargo_type == "CAR", Shipment.status == "IN_TRANSIT").with_entities(func.count(Shipment.id)).scalar() or 0

    upcoming_stats = get_upcoming_arrival_stats()

    stats = {
        "total_orders": total_orders,
        "total_cars": total_cars,
        "total_containers": total_containers,
        "containers_received": containers_received,
        "containers_not_received": containers_not_received,
        "containers_in_transit": containers_in_transit,
        "cars_received": cars_received,
        "cars_not_received": cars_not_received,
        "cars_in_transit": cars_in_transit,
        "arrivals_three_days": upcoming_stats["three_days"],
        "arrivals_week": upcoming_stats["week"],
        "arrivals_month": upcoming_stats["month"],
    }
    return render_template("dashboard.html", stats=stats)


# -------------------- Shipments List --------------------
@app.get("/shipments")
@login_required
def shipments_list():
    search_query = (request.args.get("q") or "").strip()
    arrival_filter = (request.args.get("arrival_filter") or "").strip()

    shipments_query = owner_filter(Shipment.query)

    if arrival_filter in ("3days", "week", "month"):
        start_date, end_date = arrival_window_bounds(arrival_filter)
        shipments_query = shipments_query.filter(
            Shipment.arrival_date.isnot(None),
            Shipment.arrival_date >= start_date,
            Shipment.arrival_date <= end_date,
            Shipment.status != "RECEIVED",
        )

    if search_query:
        like_value = f"%{search_query}%"

        status_keywords = {
            "NEW": ["new", "جديد", "جديدة", "طلبية", "طلبية جديدة"],
            "IN_TRANSIT": ["in_transit", "transit", "قيد", "الشحن", "قيد الشحن", "بالطريق", "بالطريق", "غير مستلمة"],
            "RECEIVED": ["received", "مستلم", "مستلمة", "واصل", "واصلة", "تم الاستلام"],
        }
        matched_statuses = [
            code
            for code, words in status_keywords.items()
            if any(word.lower() in search_query.lower() for word in words)
        ]

        shipments_query = (
            shipments_query
            .outerjoin(Factory, Shipment.factory_id == Factory.id)
            .outerjoin(Country, Shipment.country_id == Country.id)
            .filter(
                or_(
                    cast(Shipment.id, String).like(like_value),
                    Factory.name.like(like_value),
                    Country.name.like(like_value),
                    Shipment.invoice_no.like(like_value),
                    cast(Shipment.invoice_date, String).like(like_value),
                    cast(Shipment.invoice_amount, String).like(like_value),
                    Shipment.invoice_currency.like(like_value),
                    Shipment.bol_no.like(like_value),
                    Shipment.msc_tracking.like(like_value),
                    Shipment.shipping_mode.like(like_value),
                    Shipment.cargo_type.like(like_value),
                    cast(Shipment.containers_count, String).like(like_value),
                    cast(Shipment.ship_date, String).like(like_value),
                    cast(Shipment.arrival_date, String).like(like_value),
                    cast(Shipment.shipping_cost, String).like(like_value),
                    Shipment.shipping_paid_by.like(like_value),
                    Shipment.other_details.like(like_value),
                    Shipment.shipping_details.like(like_value),
                    Shipment.status.like(like_value),
                    Shipment.status.in_(matched_statuses) if matched_statuses else False,
                    cast(Shipment.created_at, String).like(like_value),
                )
            )
        )

    shipments = shipments_query.order_by(Shipment.id.asc()).all()
    return render_template(
        "shipments_list.html",
        shipments=shipments,
        search_query=search_query,
        arrival_filter=arrival_filter,
    )


# Shipment details for modal
@app.get("/shipments/<int:shipment_id>/details")
@login_required
def shipment_details(shipment_id):
    s = owner_filter(Shipment.query).filter(Shipment.id == shipment_id).first_or_404()

    status_txt, _ = status_badge(s.status)
    mode = "بري" if s.shipping_mode == "BRI" else "بحري"
    cargo = "حاوية" if s.cargo_type == "CONTAINER" else "سيارة"

    def d(dt):
        return dt.strftime("%Y-%m-%d") if dt else ""

    return jsonify({
        "id": s.id,
        "factory": s.factory.name if s.factory else "",
        "country": s.country.name if s.country else "",

        "invoice_no": s.invoice_no or "",
        "invoice_date": d(s.invoice_date),
        "invoice_amount": money(s.invoice_amount),
        "invoice_currency": s.invoice_currency or "",

        "bol_no": s.bol_no or "",
        "msc_tracking": s.msc_tracking or "",
        "manual_tracking": manual_tracking_for_shipment(s.id),
        "container_movements": container_tracking_for_shipment(s.id),

        "shipping_mode": mode,
        "cargo_type": cargo,
        "containers_count": s.containers_count if s.containers_count is not None else 0,

        "ship_date": d(s.ship_date),
        "arrival_date": d(s.arrival_date),

        "shipping_cost": money(s.shipping_cost),
        "shipping_paid_by": s.shipping_paid_by or "",

        "other_details": s.other_details or "",
        "shipping_details": s.shipping_details or "",

        "status": status_txt,
        "clearer": s.clearer.company_name if s.clearer else "",
        "carrier": s.carrier.company_name if s.carrier else "",
        "clearance_cost": money(s.clearance_cost),
        "clearance_status": clearance_status_label(s.clearance_status),
    })


# -------------------- Shipments Add/Edit/Delete --------------------
@app.route("/shipments/new", methods=["GET", "POST"])
@login_required
def shipments_new():
    factories = owner_filter(Factory.query).order_by(Factory.name.asc()).all()
    countries = owner_filter(Country.query).order_by(Country.name.asc()).all()
    favorite_clearer_ids = [f.provider_id for f in ProviderFavorite.query.filter_by(user_id=current_user.id, service_type="CLEARER").all()]
    favorite_carrier_ids = [f.provider_id for f in ProviderFavorite.query.filter_by(user_id=current_user.id, service_type="CARRIER").all()]
    clearers = User.query.filter(User.id.in_(favorite_clearer_ids), User.is_clearer == True, User.is_active == True).order_by(User.company_name.asc(), User.username.asc()).all() if favorite_clearer_ids else []
    carriers = User.query.filter(User.id.in_(favorite_carrier_ids), User.is_carrier == True, User.is_active == True).order_by(User.company_name.asc(), User.username.asc()).all() if favorite_carrier_ids else []

    if request.method == "POST":
        factory_id = request.form.get("factory_id")
        country_id = request.form.get("country_id")
        if not factory_id or not country_id or not str(factory_id).isdigit() or not str(country_id).isdigit():
            flash("يرجى اختيار اسم المصنع والبلد.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=None)

        factory = owner_filter(Factory.query).filter(Factory.id == int(factory_id)).first()
        country = owner_filter(Country.query).filter(Country.id == int(country_id)).first()
        if not factory or not country:
            flash("لا يمكن استخدام معمل أو بلد تابع لمستخدم آخر.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=None)

        selected_clearer_id = request.form.get("clearer_id", type=int) or None
        selected_carrier_id = request.form.get("carrier_id", type=int) or None
        selected_clearer = User.query.filter_by(id=selected_clearer_id, is_clearer=True, is_active=True).first() if selected_clearer_id else None
        selected_carrier = User.query.filter_by(id=selected_carrier_id, is_carrier=True, is_active=True).first() if selected_carrier_id else None
        if selected_clearer_id and not selected_clearer:
            flash("المخلص المختار غير متوفر أو غير فعّال.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=None)
        if selected_carrier_id and not selected_carrier:
            flash("شركة النقل المختارة غير متوفرة أو غير فعّالة.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=None)

        s = Shipment(
            owner_id=current_owner_id(),
            factory_id=factory.id,
            country_id=country.id,

            invoice_no=request.form.get("invoice_no") or None,
            invoice_date=parse_date(request.form.get("invoice_date")),

            invoice_amount=to_float(request.form.get("invoice_amount")),
            invoice_currency=request.form.get("invoice_currency") or "USD",

            bol_no=request.form.get("bol_no") or None,
            msc_tracking=(request.form.get("msc_tracking") or "").strip() or None,

            shipping_mode=request.form.get("shipping_mode") or "BRI",
            cargo_type=request.form.get("cargo_type") or "CONTAINER",

            containers_count=int(request.form.get("containers_count") or 0),

            ship_date=parse_date(request.form.get("ship_date")),
            arrival_date=parse_date(request.form.get("arrival_date")),

            shipping_cost=to_float(request.form.get("shipping_cost")),
            shipping_paid_by=request.form.get("shipping_paid_by") or None,

            other_details=request.form.get("other_details") or None,
            shipping_details=request.form.get("shipping_details") or None,

            clearer_id=None,
            carrier_id=None,
            clearance_company_name=None,
            clearance_cost=None,
            clearance_status="PENDING",
            clearance_quote_status="REQUESTED",
            carrier_quote_status="REQUESTED",

            status=request.form.get("status") or "NEW",
        )

        s.clearer_id = selected_clearer.id if selected_clearer else None
        s.clearance_company_name = selected_clearer.company_name if selected_clearer else None
        s.carrier_id = selected_carrier.id if selected_carrier else None

        db.session.add(s)
        db.session.flush()
        if selected_clearer:
            create_notification(selected_clearer.id, "طلب تخليص جديد", f"وصل طلب تخليص جديد من {current_user.company_name or current_user.username} للفاتورة {s.invoice_no or s.id}.", url_for("clearer_orders"))
        if selected_carrier:
            create_notification(selected_carrier.id, "طلب حجز مساحة نقل جديد", f"وصل طلب نقل جديد من {current_user.company_name or current_user.username} للفاتورة {s.invoice_no or s.id}.", url_for("carrier_orders"))
        db.session.commit()
        flash("تمت إضافة الشحنة بنجاح.", "success")
        return redirect(url_for("shipments_list"))

    return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=None)


@app.route("/shipments/<int:shipment_id>/edit", methods=["GET", "POST"])
@login_required
def shipments_edit(shipment_id):
    shipment = owner_filter(Shipment.query).filter(Shipment.id == shipment_id).first_or_404()
    factories = owner_filter(Factory.query).order_by(Factory.name.asc()).all()
    countries = owner_filter(Country.query).order_by(Country.name.asc()).all()
    favorite_clearer_ids = [f.provider_id for f in ProviderFavorite.query.filter_by(user_id=current_user.id, service_type="CLEARER").all()]
    favorite_carrier_ids = [f.provider_id for f in ProviderFavorite.query.filter_by(user_id=current_user.id, service_type="CARRIER").all()]
    if shipment.clearer_id and shipment.clearer_id not in favorite_clearer_ids:
        favorite_clearer_ids.append(shipment.clearer_id)
    if shipment.carrier_id and shipment.carrier_id not in favorite_carrier_ids:
        favorite_carrier_ids.append(shipment.carrier_id)
    clearers = User.query.filter(User.id.in_(favorite_clearer_ids), User.is_clearer == True, User.is_active == True).order_by(User.company_name.asc(), User.username.asc()).all() if favorite_clearer_ids else []
    carriers = User.query.filter(User.id.in_(favorite_carrier_ids), User.is_carrier == True, User.is_active == True).order_by(User.company_name.asc(), User.username.asc()).all() if favorite_carrier_ids else []

    if request.method == "POST":
        factory_id = request.form.get("factory_id")
        country_id = request.form.get("country_id")
        if not factory_id or not country_id or not str(factory_id).isdigit() or not str(country_id).isdigit():
            flash("يرجى اختيار اسم المصنع والبلد.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=shipment)

        factory = owner_filter(Factory.query).filter(Factory.id == int(factory_id)).first()
        country = owner_filter(Country.query).filter(Country.id == int(country_id)).first()
        if not factory or not country:
            flash("لا يمكن استخدام معمل أو بلد تابع لمستخدم آخر.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=shipment)

        shipment.factory_id = factory.id
        shipment.country_id = country.id
        shipment.owner_id = current_owner_id()

        shipment.invoice_no = request.form.get("invoice_no") or None
        shipment.invoice_date = parse_date(request.form.get("invoice_date"))

        shipment.invoice_amount = to_float(request.form.get("invoice_amount"))
        shipment.invoice_currency = request.form.get("invoice_currency") or "USD"

        carrier_controls_shipping = bool(shipment.carrier_id)
        new_carrier_id = request.form.get("carrier_id", type=int) or None
        new_clearer_id = request.form.get("clearer_id", type=int) or None
        selected_clearer = User.query.filter_by(id=new_clearer_id, is_clearer=True, is_active=True).first() if new_clearer_id else None
        selected_carrier = User.query.filter_by(id=new_carrier_id, is_carrier=True, is_active=True).first() if new_carrier_id else None
        if new_clearer_id and not selected_clearer:
            flash("المخلص المختار غير متوفر أو غير فعّال.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=shipment)
        if new_carrier_id and not selected_carrier:
            flash("شركة النقل المختارة غير متوفرة أو غير فعّالة.", "danger")
            return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=shipment)

        if not carrier_controls_shipping or new_carrier_id != shipment.carrier_id:
            shipment.bol_no = request.form.get("bol_no") or None
        shipment.msc_tracking = (request.form.get("msc_tracking") or "").strip() or None

        shipment.shipping_mode = request.form.get("shipping_mode") or "BRI"
        shipment.cargo_type = request.form.get("cargo_type") or "CONTAINER"

        shipment.containers_count = int(request.form.get("containers_count") or 0)

        if not carrier_controls_shipping or new_carrier_id != shipment.carrier_id:
            shipment.ship_date = parse_date(request.form.get("ship_date"))
            shipment.arrival_date = parse_date(request.form.get("arrival_date"))
            shipment.shipping_cost = to_float(request.form.get("shipping_cost"))
            shipment.shipping_paid_by = request.form.get("shipping_paid_by") or None

        shipment.other_details = request.form.get("other_details") or None
        if not carrier_controls_shipping or new_carrier_id != shipment.carrier_id:
            shipment.shipping_details = request.form.get("shipping_details") or None
            shipment.status = request.form.get("status") or "NEW"

        old_clearer_id = shipment.clearer_id
        old_carrier_id = shipment.carrier_id
        shipment.clearer_id = selected_clearer.id if selected_clearer else None
        shipment.clearance_company_name = selected_clearer.company_name if selected_clearer else None
        if not selected_clearer:
            shipment.clearance_cost = None
            shipment.clearance_status = "PENDING"
            shipment.clearance_quote_status = "REQUESTED"
        elif old_clearer_id != shipment.clearer_id:
            shipment.clearance_cost = None
            shipment.clearance_status = "PENDING"
            shipment.clearance_quote_status = "REQUESTED"
        shipment.carrier_id = selected_carrier.id if selected_carrier else None
        if not selected_carrier:
            shipment.carrier_quote_status = "REQUESTED"
        elif old_carrier_id != shipment.carrier_id:
            shipment.carrier_quote_status = "REQUESTED"

        if selected_clearer and old_clearer_id != shipment.clearer_id:
            create_notification(selected_clearer.id, "طلب تخليص جديد", f"وصل طلب تخليص جديد للفاتورة {shipment.invoice_no or shipment.id}.", url_for("clearer_orders"))
        if selected_carrier and old_carrier_id != shipment.carrier_id:
            create_notification(selected_carrier.id, "طلب حجز مساحة نقل جديد", f"وصل طلب نقل جديد للفاتورة {shipment.invoice_no or shipment.id}.", url_for("carrier_orders"))

        db.session.commit()
        flash("تم تعديل الشحنة بنجاح.", "success")
        return redirect(url_for("shipments_list"))

    return render_template("shipment_form.html", factories=factories, countries=countries, clearers=clearers, carriers=carriers, shipment=shipment)


@app.post("/shipments/<int:shipment_id>/delete")
@login_required
def shipments_delete(shipment_id):
    shipment = owner_filter(Shipment.query).filter(Shipment.id == shipment_id).first_or_404()
    db.session.delete(shipment)
    db.session.commit()
    flash("تم حذف الشحنة.", "success")
    return redirect(url_for("shipments_list"))


# -------------------- Manage Factories / Countries --------------------
@app.route("/manage/factories", methods=["GET", "POST"])
@login_required
def manage_factories():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("اسم المعمل مطلوب.", "danger")
            return redirect(url_for("manage_factories"))

        db.session.add(Factory(name=name, owner_id=current_owner_id()))
        db.session.commit()
        flash("تمت إضافة المعمل.", "success")
        return redirect(url_for("manage_factories"))

    factories = owner_filter(Factory.query).order_by(Factory.name.asc()).all()
    return render_template("manage_factories.html", factories=factories)


@app.post("/manage/factories/<int:factory_id>/update")
@login_required
def update_factory(factory_id):
    factory = owner_filter(Factory.query).filter(Factory.id == factory_id).first_or_404()
    name = (request.form.get("name") or "").strip()

    if not name:
        flash("اسم المعمل مطلوب.", "danger")
        return redirect(url_for("manage_factories"))

    factory.name = name
    db.session.commit()
    flash("تم تعديل المعمل بنجاح.", "success")
    return redirect(url_for("manage_factories"))


@app.post("/manage/factories/<int:factory_id>/delete")
@login_required
def delete_factory(factory_id):
    factory = owner_filter(Factory.query).filter(Factory.id == factory_id).first_or_404()

    linked_shipments_count = owner_filter(Shipment.query).filter(Shipment.factory_id == factory.id).count()
    if linked_shipments_count > 0:
        flash("لا يمكن حذف المعمل لأنه مرتبط بشحنات مسجلة. يمكنك تعديل اسمه بدلاً من الحذف.", "warning")
        return redirect(url_for("manage_factories"))

    db.session.delete(factory)
    db.session.commit()
    flash("تم حذف المعمل بنجاح.", "success")
    return redirect(url_for("manage_factories"))


@app.route("/manage/countries", methods=["GET", "POST"])
@login_required
def manage_countries():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("اسم البلد مطلوب.", "danger")
            return redirect(url_for("manage_countries"))

        db.session.add(Country(name=name, owner_id=current_owner_id()))
        db.session.commit()
        flash("تمت إضافة البلد.", "success")
        return redirect(url_for("manage_countries"))

    countries = owner_filter(Country.query).order_by(Country.name.asc()).all()
    return render_template("manage_countries.html", countries=countries)


@app.post("/manage/countries/<int:country_id>/update")
@login_required
def update_country(country_id):
    country = owner_filter(Country.query).filter(Country.id == country_id).first_or_404()
    name = (request.form.get("name") or "").strip()

    if not name:
        flash("اسم البلد مطلوب.", "danger")
        return redirect(url_for("manage_countries"))

    country.name = name
    db.session.commit()
    flash("تم تعديل البلد بنجاح.", "success")
    return redirect(url_for("manage_countries"))


@app.post("/manage/countries/<int:country_id>/delete")
@login_required
def delete_country(country_id):
    country = owner_filter(Country.query).filter(Country.id == country_id).first_or_404()

    linked_shipments_count = owner_filter(Shipment.query).filter(Shipment.country_id == country.id).count()
    if linked_shipments_count > 0:
        flash("لا يمكن حذف البلد لأنه مرتبط بشحنات مسجلة. يمكنك تعديل اسمه بدلاً من الحذف.", "warning")
        return redirect(url_for("manage_countries"))

    db.session.delete(country)
    db.session.commit()
    flash("تم حذف البلد بنجاح.", "success")
    return redirect(url_for("manage_countries"))


# -------------------- Reports + PDF --------------------
@app.route("/reports", methods=["GET", "POST"])
@login_required
def reports():
    factories = owner_filter(Factory.query).order_by(Factory.name.asc()).all()
    countries = owner_filter(Country.query).order_by(Country.name.asc()).all()

    shipments = None
    form_data = {}
    report_fields = get_user_report_fields()

    if request.method == "POST":
        form_data = request.form.to_dict(flat=True)
        q = apply_report_filters(form_data)
        shipments = q.all()

        session["report_ids"] = [s.id for s in shipments]
        session["report_filters"] = form_data
    else:
        form_data = session.get("report_filters", {}) or {}

    return render_template(
        "reports.html",
        factories=factories,
        countries=countries,
        shipments=shipments,
        form_data=form_data,
        report_fields=report_fields,
        report_field_labels=REPORT_FIELD_LABELS,
    )


@app.get("/reports/pdf")
@login_required
def reports_pdf_from_last_preview():
    ids = session.get("report_ids", []) or []
    if not ids:
        flash("لا توجد بيانات معروضة لتصديرها. استخدم زر العرض أولاً.", "warning")
        return redirect(url_for("reports"))

    shipments = owner_filter(Shipment.query).filter(Shipment.id.in_(ids)).order_by(Shipment.id.asc()).all()
    return generate_pdf(shipments, get_user_report_fields())


@app.get("/reports/reset")
@login_required
def reports_reset():
    session.pop("report_ids", None)
    session.pop("report_filters", None)
    flash("تم تصفير الحقول.", "success")
    return redirect(url_for("reports"))


def apply_report_filters(form):
    q = owner_filter(Shipment.query)

    shipment_id = (form.get("shipment_id") or "").strip()
    if shipment_id.isdigit():
        q = q.filter(Shipment.id == int(shipment_id))

    factory_id = (form.get("factory_id") or "").strip()
    if factory_id.isdigit():
        q = q.filter(Shipment.factory_id == int(factory_id))

    invoice_no = (form.get("invoice_no") or "").strip()
    if invoice_no:
        q = q.filter(Shipment.invoice_no.contains(invoice_no))

    bol_no = (form.get("bol_no") or "").strip()
    if bol_no:
        q = q.filter(Shipment.bol_no.contains(bol_no))

    inv_min = to_float(form.get("invoice_amount_min"))
    inv_max = to_float(form.get("invoice_amount_max"))
    if inv_min is not None:
        q = q.filter(Shipment.invoice_amount >= inv_min)
    if inv_max is not None:
        q = q.filter(Shipment.invoice_amount <= inv_max)

    shipping_mode = (form.get("shipping_mode") or "").strip()
    if shipping_mode in ("BRI", "BAHRI"):
        q = q.filter(Shipment.shipping_mode == shipping_mode)

    cargo_type = (form.get("cargo_type") or "").strip()
    if cargo_type in ("CAR", "CONTAINER"):
        q = q.filter(Shipment.cargo_type == cargo_type)

    status = (form.get("status") or "").strip()
    if status in ("NEW", "IN_TRANSIT", "RECEIVED"):
        q = q.filter(Shipment.status == status)

    date_from = parse_date(form.get("date_from") or "")
    date_to = parse_date(form.get("date_to") or "")
    if date_from:
        q = q.filter(Shipment.ship_date >= date_from)
    if date_to:
        q = q.filter(Shipment.ship_date <= date_to)

    return q.order_by(Shipment.id.asc())


def generate_pdf(shipments, report_fields=None):
    report_fields = clean_report_fields(report_fields) or DEFAULT_REPORT_FIELDS[:]

    buffer = BytesIO()
    page_size = landscape(A4)
    c = canvas.Canvas(buffer, pagesize=page_size)
    width, height = page_size

    font_path = os.path.join(app.root_path, "static", "fonts", "Amiri-Regular.ttf")
    has_ar_font = os.path.exists(font_path)
    if has_ar_font:
        try:
            pdfmetrics.getFont("AR")
        except Exception:
            pdfmetrics.registerFont(TTFont("AR", font_path))
        ar_font = "AR"
    else:
        ar_font = "Helvetica"

    def draw_text_right(x, y, text, font=ar_font, size=8, limit=None):
        text = safe_str(text, limit)
        c.setFont(font, size)
        c.drawRightString(x, y, ar(text) if font == "AR" else str(text))

    c.setFont(ar_font, 14)
    title = "نظام متابعة الطلبات الآلي - تقرير الشحنات"
    c.drawRightString(width - 1.5 * cm, height - 1.4 * cm, ar(title) if ar_font == "AR" else title)
    draw_text_right(width - 1.5 * cm, height - 2.0 * cm, f"تاريخ الطباعة: {datetime.now().strftime('%Y-%m-%d %H:%M')}", size=9)

    y = height - 3.0 * cm
    right_margin = 1.2 * cm
    usable_width = width - 2.4 * cm
    col_width = usable_width / max(len(report_fields) + 1, 1)
    headers = ["#"] + [REPORT_FIELD_LABELS.get(f, f) for f in report_fields]

    def draw_header(ypos):
        x = width - right_margin
        for header in headers:
            draw_text_right(x, ypos, header, size=8.2, limit=16)
            x -= col_width
        c.line(right_margin, ypos - 0.15 * cm, width - right_margin, ypos - 0.15 * cm)

    draw_header(y)
    y -= 0.55 * cm

    for s in shipments:
        if y < 1.8 * cm:
            c.showPage()
            y = height - 1.8 * cm
            draw_header(y)
            y -= 0.55 * cm

        x = width - right_margin
        draw_text_right(x, y, str(s.id), font="Helvetica", size=7.7, limit=8)
        x -= col_width
        for field in report_fields:
            value = shipment_report_value(s, field)
            font = "Helvetica" if field in {"invoice_amount", "invoice_currency", "containers_count", "shipping_cost", "arrival_date"} else ar_font
            draw_text_right(x, y, value, font=font, size=7.5, limit=18)
            x -= col_width
        y -= 0.45 * cm

    c.setFont(ar_font, 8)
    footer = "جميع الحقوق محفوظة @ المهندس علي الاميري"
    c.drawRightString(width - 1.5 * cm, 1.0 * cm, ar(footer) if ar_font == "AR" else footer)

    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="shipments_report.pdf", mimetype="application/pdf")


# ===================== Admin: Users Management =====================
def admin_required_redirect():
    if not is_admin():
        flash("غير مصرح بالدخول إلى لوحة مدير النظام.", "danger")
        return redirect(url_for("dashboard"))
    return None


def normalize_plan(plan_type: str) -> str:
    plan_type = (plan_type or "TRIAL").strip().upper()
    return plan_type if plan_type in {"TRIAL", "MONTHLY", "YEARLY", "TWO_YEARS"} else "TRIAL"


@app.get("/admin")
@login_required
def admin_home():
    return redirect(url_for("admin_users"))


@app.get("/admin/users")
@login_required
def admin_users():
    blocked = admin_required_redirect()
    if blocked:
        return blocked

    users = User.query.filter_by(is_support=False, is_advertiser=False, is_clearer=False, is_carrier=False).order_by(User.is_admin.desc(), User.id.desc()).all()
    normal_users_count = User.query.filter_by(is_admin=False, is_support=False, is_advertiser=False, is_clearer=False, is_carrier=False).count()
    active_users_count = User.query.filter_by(is_admin=False, is_support=False, is_advertiser=False, is_clearer=False, is_carrier=False, is_active=True).count()
    inactive_users_count = User.query.filter_by(is_admin=False, is_support=False, is_advertiser=False, is_clearer=False, is_carrier=False, is_active=False).count()

    return render_template(
        "admin_users.html",
        users=users,
        normal_users_count=normal_users_count,
        active_users_count=active_users_count,
        inactive_users_count=inactive_users_count,
    )


@app.route("/admin/users/new", methods=["GET", "POST"])
@login_required
def admin_users_new():
    blocked = admin_required_redirect()
    if blocked:
        return blocked

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        plan_type = normalize_plan(request.form.get("plan_type"))
        activate_now = (request.form.get("activate_now") or "") == "1"
        company_name = (request.form.get("company_name") or "").strip()
        owner_name = (request.form.get("owner_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        address = (request.form.get("address") or "").strip()

        if not username or not password:
            flash("اسم المستخدم وكلمة المرور مطلوبة.", "danger")
            return render_template("admin_user_form.html", user=None)

        if User.query.filter_by(username=username).first():
            flash("اسم المستخدم موجود مسبقاً.", "warning")
            return render_template("admin_user_form.html", user=None)

        user = User(username=username, is_admin=False, is_support=False, is_advertiser=False, is_clearer=False, is_carrier=False, advertiser_type="MERCHANT")
        user.set_password(password)
        user.license_key = generate_license_key()
        user.plan_type = plan_type
        user.starts_at = datetime.utcnow()
        user.expires_at = plan_to_expiry(plan_type)
        user.license_activated = activate_now
        user.is_active = activate_now
        user.company_name = company_name
        user.owner_name = owner_name
        user.phone = phone
        user.email = email
        user.address = address

        db.session.add(user)
        db.session.commit()

        flash("تم إنشاء المستخدم بنجاح." if activate_now else "تم إنشاء المستخدم. فعّل الحساب حتى يتمكن من الدخول.", "success")
        return redirect(url_for("admin_users"))

    return render_template("admin_user_form.html", user=None)


@app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def admin_users_edit(user_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked

    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        plan_type = normalize_plan(request.form.get("plan_type") or user.plan_type)
        company_name = (request.form.get("company_name") or "").strip()
        owner_name = (request.form.get("owner_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        address = (request.form.get("address") or "").strip()

        if not username:
            flash("اسم المستخدم مطلوب.", "danger")
            return render_template("admin_user_form.html", user=user)

        duplicate = User.query.filter(User.username == username, User.id != user.id).first()
        if duplicate:
            flash("اسم المستخدم مستخدم من حساب آخر.", "warning")
            return render_template("admin_user_form.html", user=user)

        user.username = username
        user.plan_type = plan_type
        user.company_name = company_name
        user.owner_name = owner_name
        user.phone = phone
        user.email = email
        user.address = address

        if new_password.strip():
            user.set_password(new_password.strip())

        if (request.form.get("reset_expiry") or "") == "1":
            user.starts_at = datetime.utcnow()
            user.expires_at = plan_to_expiry(user.plan_type)

        if (request.form.get("regen_license") or "") == "1":
            user.license_key = generate_license_key()
            user.license_activated = False
            user.is_active = False

        if (request.form.get("activate_license") or "") == "1":
            code = (request.form.get("license_code") or "").strip()
            if code and code == (user.license_key or ""):
                user.license_activated = True
                user.is_active = True
                user.starts_at = datetime.utcnow()
                user.expires_at = plan_to_expiry(user.plan_type)
                flash("تم تفعيل الحساب بنجاح.", "success")
            else:
                flash("كود اللايسنس غير صحيح.", "danger")

        if (request.form.get("deactivate") or "") == "1":
            if user.id == current_user.id:
                flash("لا يمكن إلغاء تفعيل حسابك الحالي.", "danger")
            else:
                user.license_activated = False
                user.is_active = False

        user.ensure_valid()
        db.session.commit()

        flash("تم حفظ بيانات المستخدم.", "success")
        return redirect(url_for("admin_users"))

    return render_template("admin_user_form.html", user=user)


@app.post("/admin/users/<int:user_id>/delete")
@login_required
def admin_users_delete(user_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id or user.is_admin:
        flash("لا يمكن حذف حساب مدير النظام.", "danger")
        return redirect(url_for("admin_users"))

    has_data = (
        Shipment.query.filter_by(owner_id=user.id).count()
        + Factory.query.filter_by(owner_id=user.id).count()
        + Country.query.filter_by(owner_id=user.id).count()
    )
    if has_data:
        flash("لا يمكن حذف المستخدم لأنه يمتلك بيانات شحنات/معامل/بلدان. عطّل الحساب بدلاً من الحذف حتى لا تضيع بياناته.", "warning")
        return redirect(url_for("admin_users"))

    db.session.delete(user)
    db.session.commit()
    flash("تم حذف المستخدم.", "success")
    return redirect(url_for("admin_users"))



@app.route("/admin/system-settings", methods=["GET"])
@login_required
def admin_system_settings():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    return redirect(url_for("admin_tracking_settings"))


@app.route("/admin/system-settings/tracking", methods=["GET", "POST"])
@login_required
def admin_tracking_settings():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    if request.method == "POST":
        flash("تم إيقاف إعدادات SeaRates. تُدار بيانات المسارات من Tracking Engine.", "info")
        return redirect(url_for("admin_tracking_settings"))
    return render_template("admin_tracking_settings.html")


@app.get("/admin/system-settings/tracking/test")
@login_required
def admin_tracking_settings_test():
    blocked = admin_required_redirect()
    if blocked:
        return jsonify({"ok": False, "error": "غير مصرح"}), 403
    return jsonify({"ok": False, "error": "تم إيقاف اتصال SeaRates الخارجي"}), 410


@app.get("/api/tracking/searates")
@login_required
def api_tracking_searates_retired():
    return jsonify({"ok": False, "error": "تم إيقاف SeaRates؛ استخدم بيانات مسارات PORTEXA"}), 410


@app.route("/admin/report-settings", methods=["GET", "POST"])
@login_required
def admin_report_settings():
    blocked = admin_required_redirect()
    if blocked:
        return blocked

    users = User.query.filter_by(is_admin=False, is_support=False, is_advertiser=False, is_clearer=False, is_carrier=False).order_by(User.username.asc()).all()
    selected_user_id = request.values.get("user_id", type=int)
    if not selected_user_id and users:
        selected_user_id = users[0].id

    selected_user = User.query.filter_by(id=selected_user_id, is_admin=False, is_support=False, is_advertiser=False, is_clearer=False, is_carrier=False).first() if selected_user_id else None

    if request.method == "POST":
        if not selected_user:
            flash("اختر مستخدم صحيح لتخصيص التقرير.", "danger")
            return redirect(url_for("admin_report_settings"))

        selected_fields = clean_report_fields(request.form.getlist("fields"))
        if not selected_fields:
            flash("اختر حقل واحد على الأقل حتى يظهر التقرير عند المستخدم.", "warning")
            return redirect(url_for("admin_report_settings", user_id=selected_user.id))

        setting = ReportSetting.query.filter_by(user_id=selected_user.id).first()
        if not setting:
            setting = ReportSetting(user_id=selected_user.id)
            db.session.add(setting)
        setting.fields_csv = ",".join(selected_fields)
        db.session.commit()

        flash(f"تم حفظ تخصيص تقرير المستخدم: {selected_user.username}", "success")
        return redirect(url_for("admin_report_settings", user_id=selected_user.id))

    selected_fields = get_user_report_fields(selected_user.id) if selected_user else []

    return render_template(
        "admin_report_settings.html",
        users=users,
        selected_user=selected_user,
        selected_fields=selected_fields,
        report_fields=REPORT_FIELDS,
    )




# ===================== Admin: Advertiser Accounts + Ads =====================
@app.get("/admin/system-users/advertisers")
@login_required
def admin_advertisers():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    advertisers = User.query.filter_by(is_advertiser=True).order_by(User.id.desc()).all()
    return render_template("admin_advertisers.html", advertisers=advertisers, advertiser_types=ADVERTISER_TYPES)


@app.route("/admin/system-users/advertisers/new", methods=["GET", "POST"])
@login_required
def admin_advertisers_new():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        advertiser_type = (request.form.get("advertiser_type") or "MERCHANT").strip()
        if advertiser_type not in ADVERTISER_TYPES:
            advertiser_type = "MERCHANT"
        if not username or not password:
            flash("اسم المستخدم وكلمة المرور مطلوبة.", "danger")
            return render_template("admin_advertiser_form.html", user=None, advertiser_types=ADVERTISER_TYPES)
        if User.query.filter_by(username=username).first():
            flash("اسم المستخدم موجود مسبقاً.", "warning")
            return render_template("admin_advertiser_form.html", user=None, advertiser_types=ADVERTISER_TYPES)
        user = User(username=username, is_admin=False, is_support=False, is_advertiser=True, advertiser_type=advertiser_type, is_active=True, license_activated=True)
        user.set_password(password)
        user.company_name = (request.form.get("company_name") or "").strip()
        user.owner_name = (request.form.get("owner_name") or "").strip()
        user.phone = (request.form.get("phone") or "").strip()
        user.email = (request.form.get("email") or "").strip()
        user.address = (request.form.get("address") or "").strip()
        user.license_key = user.license_key or generate_license_key()
        user.plan_type = "YEARLY"
        user.starts_at = datetime.utcnow()
        user.expires_at = plan_to_expiry("YEARLY")
        db.session.add(user)
        db.session.commit()
        flash("تم إنشاء حساب المعلن بنجاح.", "success")
        return redirect(url_for("admin_advertisers"))
    return render_template("admin_advertiser_form.html", user=None, advertiser_types=ADVERTISER_TYPES)


@app.route("/admin/system-users/advertisers/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def admin_advertisers_edit(user_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    user = User.query.filter_by(id=user_id, is_advertiser=True).first_or_404()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        advertiser_type = (request.form.get("advertiser_type") or user.advertiser_type or "MERCHANT").strip()
        if advertiser_type not in ADVERTISER_TYPES:
            advertiser_type = "MERCHANT"
        if not username:
            flash("اسم المستخدم مطلوب.", "danger")
            return render_template("admin_advertiser_form.html", user=user, advertiser_types=ADVERTISER_TYPES)
        duplicate = User.query.filter(User.username == username, User.id != user.id).first()
        if duplicate:
            flash("اسم المستخدم مستخدم من حساب آخر.", "warning")
            return render_template("admin_advertiser_form.html", user=user, advertiser_types=ADVERTISER_TYPES)
        user.username = username
        user.advertiser_type = advertiser_type
        user.company_name = (request.form.get("company_name") or "").strip()
        user.owner_name = (request.form.get("owner_name") or "").strip()
        user.phone = (request.form.get("phone") or "").strip()
        user.email = (request.form.get("email") or "").strip()
        user.address = (request.form.get("address") or "").strip()
        user.is_active = (request.form.get("is_active") or "") == "1"
        user.license_activated = user.is_active
        if new_password.strip():
            user.set_password(new_password.strip())
        db.session.commit()
        flash("تم حفظ بيانات المعلن.", "success")
        return redirect(url_for("admin_advertisers"))
    return render_template("admin_advertiser_form.html", user=user, advertiser_types=ADVERTISER_TYPES)


@app.get("/admin/ads")
@login_required
def admin_ads():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    flash("إدارة الإعلانات أصبحت داخل حساب المعلن. أنشئ حساب معلن من حسابات المعلنين، ثم يدخل المعلن ويرفع إعلاناته للتجار.", "info")
    return redirect(url_for("admin_advertisers"))


@app.route("/admin/ads/new", methods=["GET", "POST"])
@login_required
def admin_ads_new():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    flash("رفع الإعلانات يتم من حساب المعلن وليس من حساب مدير النظام.", "info")
    return redirect(url_for("admin_advertisers"))


@app.route("/admin/ads/<int:ad_id>/edit", methods=["GET", "POST"])
@login_required
def admin_ads_edit(ad_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    flash("تعديل الإعلان يتم من حساب المعلن صاحب الإعلان.", "info")
    return redirect(url_for("admin_advertisers"))


@app.post("/admin/ads/<int:ad_id>/archive")
@login_required
def admin_ads_archive(ad_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    ad = Advertisement.query.get_or_404(ad_id)
    ad.status = "ARCHIVED"
    db.session.commit()
    flash("تم أرشفة الإعلان.", "success")
    return redirect(url_for("admin_advertisers"))


# ===================== Advertiser Portal =====================
@app.get("/advertiser/ads")
@login_required
def advertiser_ads():
    archive_expired_ads()
    ads = Advertisement.query.filter_by(advertiser_id=current_user.id).order_by(Advertisement.id.desc()).all()
    return render_template("advertiser_ads.html", ads=ads)


@app.route("/advertiser/ads/new", methods=["GET", "POST"])
@login_required
def advertiser_ads_new():
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        duration_days = max(1, request.form.get("duration_days", type=int) or 1)
        amount = to_float(request.form.get("amount"))
        currency = (request.form.get("currency") or "IQD").strip().upper()
        if not title:
            flash("عنوان الإعلان مطلوب.", "danger")
            return render_template("advertiser_ad_form.html", ad=None, currencies=AD_CURRENCIES)
        try:
            image_path = save_ad_image(request.files.get("image"))
        except ValueError as e:
            flash(str(e), "danger")
            return render_template("advertiser_ad_form.html", ad=None, currencies=AD_CURRENCIES)
        if not image_path:
            flash("صورة الإعلان مطلوبة.", "danger")
            return render_template("advertiser_ad_form.html", ad=None, currencies=AD_CURRENCIES)
        starts_at = datetime.utcnow()
        ad = Advertisement(
            advertiser_id=current_user.id,
            title=title,
            image_path=image_path,
            link_url=(request.form.get("link_url") or "").strip() or None,
            amount=amount,
            currency=currency if currency in AD_CURRENCIES else "IQD",
            duration_days=duration_days,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(days=duration_days),
            status="ACTIVE",
        )
        db.session.add(ad)
        db.session.commit()
        flash("تم رفع الإعلان ونشره.", "success")
        return redirect(url_for("advertiser_ads"))
    return render_template("advertiser_ad_form.html", ad=None, currencies=AD_CURRENCIES)


@app.route("/advertiser/ads/<int:ad_id>/edit", methods=["GET", "POST"])
@login_required
def advertiser_ads_edit(ad_id):
    ad = Advertisement.query.filter_by(id=ad_id, advertiser_id=current_user.id).first_or_404()
    if request.method == "POST":
        if ad.status == "ARCHIVED":
            flash("لا يمكن تعديل إعلان مؤرشف.", "warning")
            return redirect(url_for("advertiser_ads"))
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("عنوان الإعلان مطلوب.", "danger")
            return render_template("advertiser_ad_form.html", ad=ad, currencies=AD_CURRENCIES)
        try:
            ad.image_path = save_ad_image(request.files.get("image"), old_path=ad.image_path)
        except ValueError as e:
            flash(str(e), "danger")
            return render_template("advertiser_ad_form.html", ad=ad, currencies=AD_CURRENCIES)
        duration_days = max(1, request.form.get("duration_days", type=int) or ad.duration_days or 1)
        ad.title = title
        ad.link_url = (request.form.get("link_url") or "").strip() or None
        ad.amount = to_float(request.form.get("amount"))
        ad.currency = (request.form.get("currency") or "IQD").strip().upper() if (request.form.get("currency") or "IQD").strip().upper() in AD_CURRENCIES else "IQD"
        ad.duration_days = duration_days
        ad.ends_at = ad.starts_at + timedelta(days=duration_days)
        db.session.commit()
        flash("تم حفظ الإعلان.", "success")
        return redirect(url_for("advertiser_ads"))
    return render_template("advertiser_ad_form.html", ad=ad, currencies=AD_CURRENCIES)




# ===================== Admin: Clearance + Carrier Accounts =====================
def _save_provider_user(user, kind, request_form, is_new=False):
    user.is_admin = False
    user.is_support = False
    user.is_advertiser = False
    user.is_clearer = kind == "clearer"
    user.is_carrier = kind == "carrier"
    user.company_name = (request_form.get("company_name") or "").strip()
    user.owner_name = (request_form.get("owner_name") or "").strip()
    user.phone = (request_form.get("phone") or "").strip()
    user.email = (request_form.get("email") or "").strip()
    user.address = (request_form.get("address") or "").strip()
    user.map_url = (request_form.get("map_url") or "").strip()
    user.is_active = (request_form.get("is_active") or ("1" if is_new else "")) == "1"
    user.license_activated = user.is_active
    user.license_key = user.license_key or generate_license_key()
    user.plan_type = "YEARLY"
    user.starts_at = user.starts_at or datetime.utcnow()
    user.expires_at = user.expires_at or plan_to_expiry("YEARLY")


@app.get("/admin/system-users/clearers")
@login_required
def admin_clearers():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    users = User.query.filter_by(is_clearer=True).order_by(User.id.desc()).all()
    return render_template("admin_service_accounts.html", users=users, kind="clearer", title="حسابات المخلصين", new_endpoint="admin_clearers_new", edit_endpoint="admin_clearers_edit")


@app.route("/admin/system-users/clearers/new", methods=["GET", "POST"])
@login_required
def admin_clearers_new():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("اسم المستخدم وكلمة المرور مطلوبة.", "danger")
            return render_template("admin_service_account_form.html", user=None, kind="clearer", title="إنشاء حساب مخلص", back_endpoint="admin_clearers")
        if User.query.filter_by(username=username).first():
            flash("اسم المستخدم موجود مسبقاً.", "warning")
            return render_template("admin_service_account_form.html", user=None, kind="clearer", title="إنشاء حساب مخلص", back_endpoint="admin_clearers")
        user = User(username=username)
        user.set_password(password)
        _save_provider_user(user, "clearer", request.form, is_new=True)
        db.session.add(user)
        db.session.commit()
        flash("تم إنشاء حساب المخلص بنجاح.", "success")
        return redirect(url_for("admin_clearers"))
    return render_template("admin_service_account_form.html", user=None, kind="clearer", title="إنشاء حساب مخلص", back_endpoint="admin_clearers")


@app.route("/admin/system-users/clearers/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def admin_clearers_edit(user_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    user = User.query.filter_by(id=user_id, is_clearer=True).first_or_404()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        if not username:
            flash("اسم المستخدم مطلوب.", "danger")
            return render_template("admin_service_account_form.html", user=user, kind="clearer", title="عرض/تعديل حساب مخلص", back_endpoint="admin_clearers")
        duplicate = User.query.filter(User.username == username, User.id != user.id).first()
        if duplicate:
            flash("اسم المستخدم مستخدم من حساب آخر.", "warning")
            return render_template("admin_service_account_form.html", user=user, kind="clearer", title="عرض/تعديل حساب مخلص", back_endpoint="admin_clearers")
        user.username = username
        if new_password.strip():
            user.set_password(new_password.strip())
        _save_provider_user(user, "clearer", request.form)
        db.session.commit()
        flash("تم حفظ بيانات المخلص.", "success")
        return redirect(url_for("admin_clearers"))
    return render_template("admin_service_account_form.html", user=user, kind="clearer", title="عرض/تعديل حساب مخلص", back_endpoint="admin_clearers")


@app.get("/admin/system-users/carriers")
@login_required
def admin_carriers():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    users = User.query.filter_by(is_carrier=True).order_by(User.id.desc()).all()
    return render_template("admin_service_accounts.html", users=users, kind="carrier", title="حسابات شركات النقل", new_endpoint="admin_carriers_new", edit_endpoint="admin_carriers_edit")


@app.route("/admin/system-users/carriers/new", methods=["GET", "POST"])
@login_required
def admin_carriers_new():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("اسم المستخدم وكلمة المرور مطلوبة.", "danger")
            return render_template("admin_service_account_form.html", user=None, kind="carrier", title="إنشاء حساب شركة نقل", back_endpoint="admin_carriers")
        if User.query.filter_by(username=username).first():
            flash("اسم المستخدم موجود مسبقاً.", "warning")
            return render_template("admin_service_account_form.html", user=None, kind="carrier", title="إنشاء حساب شركة نقل", back_endpoint="admin_carriers")
        user = User(username=username)
        user.set_password(password)
        _save_provider_user(user, "carrier", request.form, is_new=True)
        db.session.add(user)
        db.session.commit()
        flash("تم إنشاء حساب شركة النقل بنجاح.", "success")
        return redirect(url_for("admin_carriers"))
    return render_template("admin_service_account_form.html", user=None, kind="carrier", title="إنشاء حساب شركة نقل", back_endpoint="admin_carriers")


@app.route("/admin/system-users/carriers/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def admin_carriers_edit(user_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    user = User.query.filter_by(id=user_id, is_carrier=True).first_or_404()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        if not username:
            flash("اسم المستخدم مطلوب.", "danger")
            return render_template("admin_service_account_form.html", user=user, kind="carrier", title="عرض/تعديل حساب شركة نقل", back_endpoint="admin_carriers")
        duplicate = User.query.filter(User.username == username, User.id != user.id).first()
        if duplicate:
            flash("اسم المستخدم مستخدم من حساب آخر.", "warning")
            return render_template("admin_service_account_form.html", user=user, kind="carrier", title="عرض/تعديل حساب شركة نقل", back_endpoint="admin_carriers")
        user.username = username
        if new_password.strip():
            user.set_password(new_password.strip())
        _save_provider_user(user, "carrier", request.form)
        db.session.commit()
        flash("تم حفظ بيانات شركة النقل.", "success")
        return redirect(url_for("admin_carriers"))
    return render_template("admin_service_account_form.html", user=user, kind="carrier", title="عرض/تعديل حساب شركة نقل", back_endpoint="admin_carriers")

# ===================== Admin: Support Staff Management =====================
@app.get("/admin/system-users/support-users")
@login_required
def admin_support_users():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    staff = User.query.filter_by(is_support=True).order_by(User.id.desc()).all()
    return render_template("admin_support_users.html", staff=staff)


@app.route("/admin/system-users/support-users/new", methods=["GET", "POST"])
@login_required
def admin_support_users_new():
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        full_name = (request.form.get("owner_name") or "").strip()
        email = (request.form.get("email") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        if not username or not password:
            flash("اسم المستخدم وكلمة المرور مطلوبة.", "danger")
            return render_template("admin_support_user_form.html", user=None)
        if User.query.filter_by(username=username).first():
            flash("اسم المستخدم موجود مسبقاً.", "warning")
            return render_template("admin_support_user_form.html", user=None)
        user = User(username=username, is_admin=False, is_support=True, is_active=True, license_activated=True)
        user.set_password(password)
        user.owner_name = full_name
        user.email = email
        user.phone = phone
        user.license_key = user.license_key or generate_license_key()
        user.plan_type = "YEARLY"
        user.starts_at = datetime.utcnow()
        user.expires_at = plan_to_expiry("YEARLY")
        db.session.add(user)
        db.session.commit()
        flash("تم إنشاء موظف دعم فني بنجاح.", "success")
        return redirect(url_for("admin_support_users"))
    return render_template("admin_support_user_form.html", user=None)


@app.route("/admin/system-users/support-users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def admin_support_users_edit(user_id):
    blocked = admin_required_redirect()
    if blocked:
        return blocked
    user = User.query.filter_by(id=user_id, is_support=True).first_or_404()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        if not username:
            flash("اسم المستخدم مطلوب.", "danger")
            return render_template("admin_support_user_form.html", user=user)
        duplicate = User.query.filter(User.username == username, User.id != user.id).first()
        if duplicate:
            flash("اسم المستخدم مستخدم من حساب آخر.", "warning")
            return render_template("admin_support_user_form.html", user=user)
        user.username = username
        user.owner_name = (request.form.get("owner_name") or "").strip()
        user.email = (request.form.get("email") or "").strip()
        user.phone = (request.form.get("phone") or "").strip()
        user.is_active = (request.form.get("is_active") or "") == "1"
        user.license_activated = user.is_active
        if new_password.strip():
            user.set_password(new_password.strip())
        db.session.commit()
        flash("تم حفظ بيانات موظف الدعم.", "success")
        return redirect(url_for("admin_support_users"))
    return render_template("admin_support_user_form.html", user=user)


# ===================== User Support Tickets =====================
@app.get("/tickets")
@login_required
def tickets_list():
    tickets = SupportTicket.query.filter_by(user_id=current_user.id).order_by(SupportTicket.id.desc()).all()
    return render_template("tickets_list.html", tickets=tickets)


@app.route("/tickets/new", methods=["GET", "POST"])
@login_required
def tickets_new():
    if request.method == "POST":
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        if not subject:
            flash("عنوان التذكرة مطلوب.", "danger")
            return render_template("ticket_new.html")
        ticket = SupportTicket(user_id=current_user.id, subject=subject, status="OPEN")
        db.session.add(ticket)
        db.session.flush()
        attach_path = attach_name = None
        try:
            attach_path, attach_name = save_support_attachment(request.files.get("attachment"))
        except ValueError as e:
            flash(str(e), "danger")
            return render_template("ticket_new.html")
        msg = TicketMessage(ticket_id=ticket.id, sender_id=current_user.id, body=body or "تم فتح التذكرة.", attachment_path=attach_path, attachment_name=attach_name)
        db.session.add(msg)
        db.session.commit()
        flash("تم فتح تذكرة الدعم بنجاح.", "success")
        return redirect(url_for("ticket_view", ticket_id=ticket.id))
    return render_template("ticket_new.html")


@app.route("/tickets/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def ticket_view(ticket_id):
    ticket = SupportTicket.query.filter_by(id=ticket_id, user_id=current_user.id).first_or_404()
    if request.method == "POST":
        if ticket.status == "CLOSED":
            flash("هذه التذكرة مغلقة. يمكنك فتح تذكرة جديدة.", "warning")
            return redirect(url_for("ticket_view", ticket_id=ticket.id))
        body = (request.form.get("body") or "").strip()
        try:
            attach_path, attach_name = save_support_attachment(request.files.get("attachment"))
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("ticket_view", ticket_id=ticket.id))
        if not body and not attach_path:
            flash("اكتب رسالة أو أرفق صورة.", "warning")
            return redirect(url_for("ticket_view", ticket_id=ticket.id))
        db.session.add(TicketMessage(ticket_id=ticket.id, sender_id=current_user.id, body=body, attachment_path=attach_path, attachment_name=attach_name))
        db.session.commit()
        return redirect(url_for("ticket_view", ticket_id=ticket.id))
    return render_template("ticket_view.html", ticket=ticket, support_mode=False)


# ===================== Support Staff Tickets =====================
@app.get("/support/tickets")
@login_required
def support_tickets():
    status = (request.args.get("status") or "OPEN").upper()
    q = SupportTicket.query
    if status in {"OPEN", "CLOSED"}:
        q = q.filter_by(status=status)
    tickets = q.order_by(SupportTicket.id.desc()).all()
    return render_template("support_tickets.html", tickets=tickets, status=status)


@app.route("/support/tickets/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def support_ticket_view(ticket_id):
    ticket = SupportTicket.query.get_or_404(ticket_id)
    if request.method == "POST":
        action = (request.form.get("action") or "message").strip()
        if action == "close":
            ticket.status = "CLOSED"
            ticket.closed_at = datetime.utcnow()
            ticket.assigned_to_id = ticket.assigned_to_id or current_user.id
            db.session.add(TicketMessage(ticket_id=ticket.id, sender_id=current_user.id, body="تم إغلاق التذكرة من قبل الدعم الفني."))
            db.session.commit()
            flash("تم إغلاق التذكرة.", "success")
            return redirect(url_for("support_ticket_view", ticket_id=ticket.id))
        if ticket.status == "CLOSED":
            flash("لا يمكن الرد على تذكرة مغلقة.", "warning")
            return redirect(url_for("support_ticket_view", ticket_id=ticket.id))
        body = (request.form.get("body") or "").strip()
        try:
            attach_path, attach_name = save_support_attachment(request.files.get("attachment"))
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("support_ticket_view", ticket_id=ticket.id))
        if not body and not attach_path:
            flash("اكتب رسالة أو أرفق صورة.", "warning")
            return redirect(url_for("support_ticket_view", ticket_id=ticket.id))
        ticket.assigned_to_id = ticket.assigned_to_id or current_user.id
        db.session.add(TicketMessage(ticket_id=ticket.id, sender_id=current_user.id, body=body, attachment_path=attach_path, attachment_name=attach_name))
        db.session.commit()
        return redirect(url_for("support_ticket_view", ticket_id=ticket.id))
    return render_template("ticket_view.html", ticket=ticket, support_mode=True)




# ===================== Notifications =====================
@app.get("/notifications")
@login_required
def notifications_list():
    if is_admin():
        return redirect(url_for("admin_users"))
    notifications = SystemNotification.query.filter_by(user_id=current_user.id).order_by(SystemNotification.id.desc()).limit(100).all()
    return render_template("notifications.html", notifications=notifications)


@app.get("/notifications/<int:notification_id>/open")
@login_required
def notification_open(notification_id):
    n = SystemNotification.query.filter_by(id=notification_id, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    return redirect(n.url or url_for("notifications_list"))


# ===================== Trader Quote Decisions =====================
@app.post("/shipments/<int:shipment_id>/clearance-quote/<decision>")
@login_required
def clearance_quote_decision(shipment_id, decision):
    shipment = owner_filter(Shipment.query).filter(Shipment.id == shipment_id).first_or_404()
    if decision not in {"accept", "reject"} or not shipment.clearer_id:
        flash("الإجراء غير صحيح.", "danger")
        return redirect(url_for("shipments_edit", shipment_id=shipment.id))
    if shipment.clearance_quote_status != "PROPOSED" or shipment.clearance_cost is None:
        flash("لا يوجد مبلغ تخليص بانتظار الموافقة.", "warning")
        return redirect(url_for("shipments_edit", shipment_id=shipment.id))
    if decision == "accept":
        shipment.clearance_quote_status = "ACCEPTED"
        shipment.clearance_status = "ACCEPTED"
        create_notification(shipment.clearer_id, "تم قبول مبلغ التخليص", f"قبل التاجر مبلغ التخليص للفاتورة {shipment.invoice_no or shipment.id}.", url_for("clearer_orders"))
        flash("تمت الموافقة على مبلغ التخليص.", "success")
    else:
        shipment.clearance_quote_status = "REJECTED"
        shipment.clearance_status = "REJECTED"
        create_notification(shipment.clearer_id, "تم رفض مبلغ التخليص", f"رفض التاجر مبلغ التخليص للفاتورة {shipment.invoice_no or shipment.id}. يمكنك إرسال مبلغ جديد.", url_for("clearer_orders"))
        flash("تم رفض مبلغ التخليص.", "warning")
    db.session.commit()
    return redirect(url_for("shipments_edit", shipment_id=shipment.id))


@app.post("/shipments/<int:shipment_id>/carrier-quote/<decision>")
@login_required
def carrier_quote_decision(shipment_id, decision):
    shipment = owner_filter(Shipment.query).filter(Shipment.id == shipment_id).first_or_404()
    if decision not in {"accept", "reject"} or not shipment.carrier_id:
        flash("الإجراء غير صحيح.", "danger")
        return redirect(url_for("shipments_edit", shipment_id=shipment.id))
    if shipment.carrier_quote_status != "PROPOSED" or shipment.shipping_cost is None:
        flash("لا يوجد مبلغ شحن بانتظار الموافقة.", "warning")
        return redirect(url_for("shipments_edit", shipment_id=shipment.id))
    if decision == "accept":
        shipment.carrier_quote_status = "ACCEPTED"
        create_notification(shipment.carrier_id, "تم قبول مبلغ النقل", f"قبل التاجر مبلغ النقل للفاتورة {shipment.invoice_no or shipment.id}.", url_for("carrier_orders"))
        flash("تمت الموافقة على مبلغ النقل.", "success")
    else:
        shipment.carrier_quote_status = "REJECTED"
        create_notification(shipment.carrier_id, "تم رفض مبلغ النقل", f"رفض التاجر مبلغ النقل للفاتورة {shipment.invoice_no or shipment.id}. يمكنك تحديث السعر.", url_for("carrier_orders"))
        flash("تم رفض مبلغ النقل.", "warning")
    db.session.commit()
    return redirect(url_for("shipments_edit", shipment_id=shipment.id))


# ===================== Trader Services Directory + Ratings =====================
@app.get("/services/clearers")
@login_required
def service_clearers():
    qtext = (request.args.get("q") or "").strip()
    q = User.query.filter_by(is_clearer=True, is_active=True)
    if qtext:
        like = f"%{qtext}%"
        q = q.filter(or_(User.company_name.ilike(like), User.owner_name.ilike(like), User.phone.ilike(like), User.email.ilike(like), User.address.ilike(like)))
    providers = q.order_by(User.company_name.asc(), User.username.asc()).all()
    return render_template("service_directory.html", providers=providers, service_type="CLEARER", title="خدمات التخليص", q=qtext)


@app.get("/services/carriers")
@login_required
def service_carriers():
    qtext = (request.args.get("q") or "").strip()
    q = User.query.filter_by(is_carrier=True, is_active=True)
    if qtext:
        like = f"%{qtext}%"
        q = q.filter(or_(User.company_name.ilike(like), User.owner_name.ilike(like), User.phone.ilike(like), User.email.ilike(like), User.address.ilike(like)))
    providers = q.order_by(User.company_name.asc(), User.username.asc()).all()
    return render_template("service_directory.html", providers=providers, service_type="CARRIER", title="خدمات النقل", q=qtext)


@app.post("/services/<service_type>/<int:provider_id>/favorite")
@login_required
def toggle_provider_favorite(service_type, provider_id):
    service_type = service_type.upper()
    if service_type not in {"CLEARER", "CARRIER"}:
        flash("نوع الخدمة غير صحيح.", "danger")
        return redirect(url_for("dashboard"))
    provider = User.query.filter(User.id == provider_id, User.is_active == True).first_or_404()
    if service_type == "CLEARER" and not provider.is_clearer:
        flash("المزود المختار ليس مخلصاً.", "danger")
        return redirect(url_for("service_clearers"))
    if service_type == "CARRIER" and not provider.is_carrier:
        flash("المزود المختار ليس شركة نقل.", "danger")
        return redirect(url_for("service_carriers"))
    fav = ProviderFavorite.query.filter_by(user_id=current_user.id, provider_id=provider.id, service_type=service_type).first()
    if fav:
        db.session.delete(fav)
        flash("تمت إزالة المزود من المفضلة.", "warning")
    else:
        db.session.add(ProviderFavorite(user_id=current_user.id, provider_id=provider.id, service_type=service_type))
        flash("تمت إضافة المزود إلى المفضلة.", "success")
    db.session.commit()
    return redirect(request.referrer or url_for("service_clearers" if service_type == "CLEARER" else "service_carriers"))


@app.route("/shipments/<int:shipment_id>/rate/<service_type>", methods=["GET", "POST"])
@login_required
def shipment_rate_service(shipment_id, service_type):
    service_type = service_type.upper()
    if service_type not in {"CLEARER", "CARRIER"}:
        flash("نوع التقييم غير صحيح.", "danger")
        return redirect(url_for("shipments_list"))
    shipment = owner_filter(Shipment.query).filter(Shipment.id == shipment_id).first_or_404()
    provider = shipment.clearer if service_type == "CLEARER" else shipment.carrier
    is_done = (shipment.clearance_status == "DONE") if service_type == "CLEARER" else (shipment.status == "RECEIVED")
    if not provider or not is_done:
        flash("لا يمكن التقييم إلا بعد إنجاز الخدمة.", "warning")
        return redirect(url_for("shipments_list"))
    existing = ServiceRating.query.filter_by(shipment_id=shipment.id, provider_id=provider.id, user_id=current_user.id, service_type=service_type).first()
    if request.method == "POST":
        stars = min(5, max(1, request.form.get("stars", type=int) or 5))
        comment = (request.form.get("comment") or "").strip()
        if existing:
            existing.stars = stars
            existing.comment = comment
        else:
            db.session.add(ServiceRating(shipment_id=shipment.id, provider_id=provider.id, user_id=current_user.id, service_type=service_type, stars=stars, comment=comment))
        db.session.commit()
        flash("تم حفظ التقييم.", "success")
        return redirect(url_for("shipments_list"))
    return render_template("rate_service.html", shipment=shipment, provider=provider, service_type=service_type, existing=existing)


# ===================== Clearer Portal =====================
@app.get("/clearer")
@login_required
def clearer_dashboard():
    q = Shipment.query.filter_by(clearer_id=current_user.id)
    stats = {
        "waiting": q.filter(Shipment.clearance_status.in_(["PENDING", "ACCEPTED"])).with_entities(func.coalesce(func.sum(Shipment.containers_count), 0)).scalar() or 0,
        "clearing": q.filter_by(clearance_status="CLEARING").with_entities(func.coalesce(func.sum(Shipment.containers_count), 0)).scalar() or 0,
        "done": q.filter_by(clearance_status="DONE").with_entities(func.coalesce(func.sum(Shipment.containers_count), 0)).scalar() or 0,
        "orders": q.count(),
    }
    return render_template("provider_dashboard.html", provider_kind="clearer", stats=stats)


@app.route("/clearer/orders", methods=["GET", "POST"])
@login_required
def clearer_orders():
    q = Shipment.query.filter_by(clearer_id=current_user.id)
    status = (request.args.get("status") or "ACTIVE").upper()
    if status == "ARCHIVE":
        q = q.filter_by(clearance_status="DONE")
    else:
        q = q.filter(Shipment.clearance_status != "DONE")
    shipments = q.order_by(Shipment.id.desc()).all()
    return render_template("clearer_orders.html", shipments=shipments, status=status, statuses=CLEARANCE_STATUSES)


@app.route("/clearer/orders/<int:shipment_id>/update", methods=["POST"])
@login_required
def clearer_order_update(shipment_id):
    shipment = Shipment.query.filter_by(id=shipment_id, clearer_id=current_user.id).first_or_404()
    new_status = (request.form.get("clearance_status") or shipment.clearance_status or "PENDING").strip().upper()
    old_status = shipment.clearance_status
    old_cost = shipment.clearance_cost
    new_cost = to_float(request.form.get("clearance_cost"))
    if new_status in CLEARANCE_STATUSES:
        shipment.clearance_status = new_status
    shipment.clearance_cost = new_cost
    if new_cost is not None and new_cost != old_cost:
        shipment.clearance_quote_status = "PROPOSED"
    create_notification(shipment.owner_id, "تحديث على التخليص", f"تم تحديث تخليص الفاتورة {shipment.invoice_no or shipment.id}: السعر {money(new_cost) if new_cost is not None else '-'}، الحالة {clearance_status_label(shipment.clearance_status)}.", url_for("shipments_edit", shipment_id=shipment.id))
    db.session.commit()
    flash("تم تحديث بيانات التخليص وإرسال إشعار للتاجر.", "success")
    return redirect(url_for("clearer_orders"))


@app.get("/clearer/financial-report")
@login_required
def clearer_financial_report():
    company = (request.args.get("company") or "").strip()
    q = Shipment.query.filter_by(clearer_id=current_user.id, clearance_status="DONE")
    if company:
        q = q.join(User, Shipment.owner_id == User.id).filter(User.company_name.contains(company))
    shipments = q.order_by(Shipment.id.desc()).all()
    total = sum((s.clearance_cost or 0) for s in shipments)
    return render_template("provider_financial_report.html", provider_kind="clearer", shipments=shipments, total=total, company=company)


# ===================== Carrier Portal =====================
@app.get("/carrier")
@login_required
def carrier_dashboard():
    q = Shipment.query.filter_by(carrier_id=current_user.id)
    stats = {
        "waiting": q.filter_by(status="NEW").with_entities(func.coalesce(func.sum(Shipment.containers_count), 0)).scalar() or 0,
        "clearing": q.filter_by(status="IN_TRANSIT").with_entities(func.coalesce(func.sum(Shipment.containers_count), 0)).scalar() or 0,
        "done": q.filter_by(status="RECEIVED").with_entities(func.coalesce(func.sum(Shipment.containers_count), 0)).scalar() or 0,
        "orders": q.count(),
    }
    return render_template("provider_dashboard.html", provider_kind="carrier", stats=stats)


@app.get("/carrier/orders")
@login_required
def carrier_orders():
    status = (request.args.get("status") or "ACTIVE").upper()
    q = Shipment.query.filter_by(carrier_id=current_user.id)
    if status == "ARCHIVE":
        q = q.filter_by(status="RECEIVED")
    else:
        q = q.filter(Shipment.status != "RECEIVED")
    shipments = q.order_by(Shipment.id.desc()).all()
    return render_template("carrier_orders.html", shipments=shipments, status=status)


@app.post("/carrier/orders/<int:shipment_id>/update")
@login_required
def carrier_order_update(shipment_id):
    shipment = Shipment.query.filter_by(id=shipment_id, carrier_id=current_user.id).first_or_404()
    shipment.bol_no = (request.form.get("bol_no") or "").strip() or None
    shipment.shipping_cost = to_float(request.form.get("shipping_cost"))
    shipment.shipping_paid_by = current_user.company_name or current_user.username
    shipment.ship_date = parse_date(request.form.get("ship_date"))
    shipment.arrival_date = parse_date(request.form.get("arrival_date"))
    old_cost = shipment.shipping_cost
    shipment.shipping_details = (request.form.get("shipping_details") or "").strip() or None
    st = (request.form.get("status") or shipment.status or "NEW").strip().upper()
    if st in {"NEW", "IN_TRANSIT", "RECEIVED"}:
        shipment.status = st
    if shipment.shipping_cost is not None and shipment.shipping_cost != old_cost:
        shipment.carrier_quote_status = "PROPOSED"
    create_notification(shipment.owner_id, "تحديث على النقل", f"تم تحديث نقل الفاتورة {shipment.invoice_no or shipment.id}: مبلغ الشحن {money(shipment.shipping_cost) if shipment.shipping_cost is not None else '-'}، الحالة {status_badge(shipment.status)[0]}.", url_for("shipments_edit", shipment_id=shipment.id))
    db.session.commit()
    flash("تم تحديث بيانات النقل وإرسال إشعار للتاجر.", "success")
    return redirect(url_for("carrier_orders"))


@app.get("/carrier/financial-report")
@login_required
def carrier_financial_report():
    company = (request.args.get("company") or "").strip()
    q = Shipment.query.filter_by(carrier_id=current_user.id, status="RECEIVED")
    if company:
        q = q.join(User, Shipment.owner_id == User.id).filter(User.company_name.contains(company))
    shipments = q.order_by(Shipment.id.desc()).all()
    total = sum((s.shipping_cost or 0) for s in shipments)
    return render_template("provider_financial_report.html", provider_kind="carrier", shipments=shipments, total=total, company=company)

# -------------------- Run --------------------
if __name__ == "__main__":
    # يسمح بالدخول من أجهزة الشبكة
    app.run(host="0.0.0.0", port=5000, debug=True)