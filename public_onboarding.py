"""Public landing and Google OpenID Connect trial onboarding for PORTEXA.

Requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI in server env.
Uses server-side OAuth code exchange and Google userinfo, never client-provided email.
"""
import os
import secrets
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user
from sqlalchemy import func

ROLE_LABELS = {"merchant": "تاجر", "clearer": "مخلص كمركي", "carrier": "وسيط شحن"}
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
        if user.is_clearer:
            return url_for("clearer_dashboard")
        if user.is_carrier:
            return url_for("carrier_dashboard")
        return url_for("dashboard")

    @bp.get("/welcome")
    def landing():
        return render_template("public_landing.html", google_ready=all(google_settings()))

    @bp.get("/join/google")
    def google_start():
        if current_user.is_authenticated:
            return redirect(destination(current_user))
        role = request.args.get("role", "")
        if role not in ROLE_LABELS:
            return redirect(url_for("onboarding.landing"))
        client_id, secret, callback = google_settings()
        if not all((client_id, secret, callback)):
            flash("التسجيل عبر Google غير متاح حالياً. يرجى المحاولة لاحقاً.", "warning")
            return redirect(url_for("onboarding.landing"))
        if not callback.startswith("https://"):
            abort(503)
        state = secrets.token_urlsafe(32)
        session["onboarding_oauth"] = {"state": state, "role": role, "created": datetime.utcnow().timestamp()}
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
            # Prevent duplicate identity creation for an email already registered by other means.
            existing = User.query.filter(func.lower(User.email) == email).first()
            if existing:
                flash("هذا البريد مرتبط بحساب سابق. سجل الدخول بالطريقة المعتادة.", "warning")
                return redirect(url_for("login"))
            role = pending["role"]
            user = User(username=username, is_admin=False, is_support=False, is_advertiser=False,
                        is_clearer=(role == "clearer"), is_carrier=(role == "carrier"),
                        advertiser_type="MERCHANT", email=email,
                        owner_name=str(profile.get("name", ""))[:200],
                        plan_type="TRIAL", starts_at=datetime.utcnow(),
                        expires_at=plan_to_expiry("TRIAL"), license_key=generate_license_key(),
                        license_activated=True, is_active=True)
            user.set_password(secrets.token_urlsafe(48))
            try:
                db.session.add(user)
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Could not create Google trial user")
                flash("تعذر إنشاء الحساب حالياً. حاول لاحقاً.", "danger")
                return redirect(url_for("onboarding.landing"))
        login_user(user)
        return redirect(destination(user))

    @app.before_request
    def public_home_redirect():
        if request.path == "/" and request.method == "GET" and not current_user.is_authenticated:
            return redirect(url_for("onboarding.landing"))

    app.register_blueprint(bp)
    return bp
