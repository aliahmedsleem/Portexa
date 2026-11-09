"""Public landing and Google OpenID Connect trial onboarding for PORTEXA.

Requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI in server env.
Uses server-side OAuth code exchange and Google userinfo, never client-provided email.
"""
import os
import re
import secrets
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user
from sqlalchemy import func

ROLE_LABELS = {"merchant": "تاجر", "clearer": "مخلص كمركي", "carrier": "وسيط شحن"}
GOVERNORATES = (
    "بغداد", "البصرة", "نينوى", "أربيل", "السليمانية", "دهوك",
    "كركوك", "الأنبار", "بابل", "كربلاء", "النجف", "القادسية",
    "واسط", "ديالى", "صلاح الدين", "ذي قار", "ميسان", "المثنى", "حلبجة"
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def register_public_onboarding(app, db, User, generate_license_key, plan_to_expiry):
    bp = Blueprint("onboarding", __name__)

    def google_settings():
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        callback = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
        return client_id, secret, callback

    def destination(user):
        if user.is_admin:
            return url_for("billing.dashboard")
        if user.is_support:
            return url_for("support_tickets")
        if getattr(user, "is_advertiser", False):
            return url_for("advertiser_ads")
        if user.is_clearer:
            return url_for("clearer_dashboard")
        if user.is_carrier:
            return url_for("carrier_dashboard")
        return url_for("dashboard")

    @bp.get("/welcome")
    def landing():
        if current_user.is_authenticated:
            return redirect(destination(current_user))
        return render_template("public_landing.html", google_ready=all(google_settings()))

    @bp.get("/login/google")
    def google_login_start():
        return start_google_oauth(mode="login")

    @bp.get("/join/google")
    def google_start():
        return start_google_oauth(mode="register")

    def start_google_oauth(mode):
        if current_user.is_authenticated:
            return redirect(destination(current_user))
        role = request.args.get("role", "")
        if mode == "register" and role not in ROLE_LABELS:
            return redirect(url_for("onboarding.landing"))
        client_id, secret, callback = google_settings()
        if not all((client_id, secret, callback)):
            flash("التسجيل عبر Google غير متاح حالياً. يرجى المحاولة لاحقاً.", "warning")
            return redirect(url_for("onboarding.landing"))
        if not callback.startswith("https://"):
            abort(503)
        state = secrets.token_urlsafe(32)
        session["onboarding_oauth"] = {"state": state, "role": role, "mode": mode, "created": datetime.utcnow().timestamp()}
        params = {"client_id": client_id, "redirect_uri": callback, "response_type": "code",
                  "scope": "openid email profile", "state": state, "prompt": "select_account"}
        return redirect(AUTH_URL + "?" + urlencode(params))

    @bp.get("/join/google/callback")
    def google_callback():
        pending = session.pop("onboarding_oauth", None)
        if not pending or datetime.utcnow().timestamp() - pending.get("created", 0) > 600:
            abort(400)
        if not secrets.compare_digest(request.args.get("state", ""), pending["state"]):
            abort(400)
        if request.args.get("error"):
            flash("تم إلغاء التسجيل عبر Google.", "warning")
            return redirect(url_for("onboarding.landing"))
        code = request.args.get("code", "")
        client_id, client_secret, callback = google_settings()
        if not code or not all((client_id, client_secret, callback)):
            abort(400)
        try:
            token_response = requests.post(TOKEN_URL, data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": callback, "grant_type": "authorization_code"
            }, timeout=12)
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
            info_response = requests.get(USERINFO_URL,
                headers={"Authorization": "Bearer " + access_token}, timeout=12)
            info_response.raise_for_status()
            profile = info_response.json()
        except (requests.RequestException, ValueError, KeyError):
            current_app.logger.exception("Google sign-in failed")
            flash("تعذر التحقق من حساب Google. حاول مرة أخرى.", "danger")
            return redirect(url_for("onboarding.landing"))

        sub = str(profile.get("sub", ""))
        email = str(profile.get("email", "")).strip().lower()
        if not sub or not email or profile.get("email_verified") is not True:
            abort(403)

        # Existing accounts are not silently linked by matching email.
        # Use a stable Google-sub-derived username for new accounts.
        username = "google_" + sub
        user = User.query.filter_by(username=username).first()
        if user:
            if user.email != email:
                abort(403)
            if not user.license_activated or not user.is_active or (user.expires_at and user.expires_at < datetime.utcnow()):
                flash("الحساب التجريبي منتهي أو غير مفعل. تواصل مع الإدارة.", "warning")
                return redirect(url_for("login"))
        else:
            if pending.get("mode") == "login":
                flash("لا يوجد حساب PORTEXA مرتبط بهذا الحساب في Google. يرجى التسجيل أولاً.", "warning")
                return redirect(url_for("onboarding.landing"))
            existing = User.query.filter(func.lower(User.email) == email).first()
            if existing:
                flash("هذا البريد مرتبط بحساب سابق. سجل الدخول بالطريقة المعتادة.", "warning")
                return redirect(url_for("login"))
            session["onboarding_profile"] = {
                "sub": sub, "email": email, "role": pending["role"],
                "created": datetime.utcnow().timestamp(),
                "csrf": secrets.token_urlsafe(32)
            }
            return redirect(url_for("onboarding.complete_profile"))
        login_user(user, remember=True)
        return redirect(destination(user))

    @bp.route("/join/complete", methods=["GET", "POST"])
    def complete_profile():
        pending = session.get("onboarding_profile")
        if not pending or datetime.utcnow().timestamp() - pending.get("created", 0) > 1200:
            session.pop("onboarding_profile", None)
            flash("انتهت جلسة التسجيل. ابدأ مرة أخرى عبر Google.", "warning")
            return redirect(url_for("onboarding.landing"))
        if current_user.is_authenticated:
            return redirect(destination(current_user))
        errors = []
        values = {}
        if request.method == "POST":
            if not secrets.compare_digest(request.form.get("csrf", ""), pending["csrf"]):
                abort(400)
            fields = ("full_name", "phone", "company_name", "business_name", "governorate", "city", "address")
            values = {key: " ".join((request.form.get(key) or "").strip().split()) for key in fields}
            full_name = values["full_name"]
            parts = full_name.split()
            if not 3 <= len(parts) <= 6 or len(full_name) > 200 or not all(
                len(p) >= 2 and all(ch.isalpha() or ch in "-'" for ch in p) for p in parts
            ):
                errors.append("الاسم الثلاثي يجب أن يتكون من ثلاثة أسماء صحيحة على الأقل.")
            phone = re.sub(r"[\s\-()]", "", values["phone"])
            if phone.startswith("+964"):
                phone = "0" + phone[4:]
            elif phone.startswith("00964"):
                phone = "0" + phone[5:]
            if not re.fullmatch(r"07[3-9][0-9]{8}", phone):
                errors.append("أدخل رقم هاتف عراقي صحيح مثل 07701234567.")
            values["phone"] = phone
            for key, label, limit in (
                ("company_name", "اسم الشركة أو المكتب", 200),
                ("business_name", "الاسم التجاري أو اسم الخدمة", 150),
                ("city", "المدينة أو منطقة العمل", 100),
                ("address", "عنوان العمل", 300),
            ):
                if len(values[key]) < 3 or len(values[key]) > limit:
                    errors.append(f"{label} مطلوب ويجب أن يتكون من 3 إلى {limit} حرف.")
            if values["governorate"] not in GOVERNORATES:
                errors.append("اختر محافظة عراقية صحيحة.")
            if not errors:
                username = "google_" + pending["sub"]
                if User.query.filter_by(username=username).first() or User.query.filter(
                    func.lower(User.email) == pending["email"]).first():
                    errors.append("يوجد حساب مسجل بهذه الهوية أو البريد الإلكتروني.")
                else:
                    user = User(
                        username=username, is_admin=False, is_support=False, is_advertiser=False,
                        is_clearer=(pending["role"] == "clearer"),
                        is_carrier=(pending["role"] == "carrier"),
                        advertiser_type="MERCHANT",
                        email=pending["email"], owner_name=full_name,
                        phone=phone, company_name=values["company_name"],
                        address=values["governorate"] + " - " + values["city"] + " - " + values["address"] + " | النشاط: " + values["business_name"],
                        plan_type="TRIAL", starts_at=datetime.utcnow(),
                        expires_at=plan_to_expiry("TRIAL"),
                        license_key=generate_license_key(),
                        license_activated=True, is_active=True,
                    )
                    # Existing User schema has no dedicated business_name column; retain it in address metadata.
                    user.set_password(secrets.token_urlsafe(48))
                    try:
                        db.session.add(user)
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                        current_app.logger.exception("Google trial onboarding failed")
                        errors.append("تعذر حفظ التسجيل. حاول مرة أخرى.")
                    else:
                        session.pop("onboarding_profile", None)
                        login_user(user, remember=True)
                        return redirect(destination(user))
        return render_template("public_complete_profile.html",
                               pending=pending, role_label=ROLE_LABELS[pending["role"]],
                               governorates=GOVERNORATES, errors=errors, values=values)

    @app.before_request
    def public_home_redirect():
        if request.path == "/" and request.method == "GET" and not current_user.is_authenticated:
            return redirect(url_for("onboarding.landing"))

    app.register_blueprint(bp)
    return bp
