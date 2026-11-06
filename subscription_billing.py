"""PORTEXA subscription ledger: opt-in integration, no automatic charges.
Register with register_billing(app, db, User) after legacy routes are defined.
All amounts are integer IQD. Payment confirmation is manual and auditable.
"""
from datetime import datetime, timedelta
import secrets
import hmac
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from flask_login import login_required, current_user
from sqlalchemy import func, Index

def register_billing(app, db, User):
    class SubscriptionInvoice(db.Model):
        __tablename__ = "subscription_invoice"
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        plan_type = db.Column(db.String(30), nullable=False)
        amount_iqd = db.Column(db.BigInteger, nullable=False)
        status = db.Column(db.String(16), nullable=False, default="UNPAID")
        due_at = db.Column(db.DateTime, nullable=False)
        issued_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        paid_at = db.Column(db.DateTime)
        confirmed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
        payment_reference = db.Column(db.String(150))
        period_starts_at = db.Column(db.DateTime)
        period_ends_at = db.Column(db.DateTime)
        __table_args__ = (Index('uq_billing_open_invoice', 'user_id', unique=True, sqlite_where=db.text("status = 'UNPAID'"), postgresql_where=db.text("status = 'UNPAID'")),)

    class SubscriptionPrice(db.Model):
        __tablename__ = "subscription_price"
        plan_type = db.Column(db.String(30), primary_key=True)
        amount_iqd = db.Column(db.BigInteger, nullable=False)
        updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    bp = Blueprint("billing", __name__, url_prefix="/admin/billing")
    @bp.before_request
    def csrf_guard():
        if request.method == "POST":
            expected = session.get("billing_csrf")
            supplied = request.form.get("billing_csrf", "")
            if not expected or not hmac.compare_digest(expected, supplied):
                abort(400)

    plans = ("MONTHLY", "YEARLY", "TWO_YEARS")
    labels = {"TRIAL": "تجريبي", "MONTHLY": "شهري", "YEARLY": "سنوي", "TWO_YEARS": "سنتان"}
    def eligible_query():
        return User.query.filter(
            User.is_admin.is_(False), User.is_support.is_(False),
            User.is_advertiser.is_(False), User.username.notin_(("admin", "systemadmin"))
        )
    def admin_only():
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
    def price_map():
        return {p.plan_type: p.amount_iqd for p in SubscriptionPrice.query.all()}
    def advance(start, plan):
        # Calendar-aware month/year renewal rather than fixed 30/365 days.
        from calendar import monthrange
        months = {"MONTHLY": 1, "YEARLY": 12, "TWO_YEARS": 24}[plan]
        y = start.year + (start.month - 1 + months) // 12
        m = (start.month - 1 + months) % 12 + 1
        return start.replace(year=y, month=m, day=min(start.day, monthrange(y, m)[1]))
    def totals(status):
        return int(db.session.query(func.coalesce(func.sum(SubscriptionInvoice.amount_iqd), 0)).filter(
            SubscriptionInvoice.status == status).scalar() or 0)

    @bp.get("/")
    @login_required
    def dashboard():
        admin_only()
        now = datetime.utcnow()
        session.setdefault('billing_csrf', secrets.token_urlsafe(32))
        users = eligible_query().all()
        merchants = [u for u in users if not u.is_clearer and not u.is_carrier]
        clearers = [u for u in users if u.is_clearer]
        carriers = [u for u in users if u.is_carrier]
        invoices = SubscriptionInvoice.query.order_by(SubscriptionInvoice.issued_at.desc()).limit(100).all()
        lookup = {u.id: u for u in users}
        prices = price_map()
        next_30 = now + timedelta(days=30)
        # Only forecast users with an actual expiry in the window, not unknown future sales.
        forecast = sum(prices.get(u.plan_type, 0) for u in users
                       if u.license_activated and u.is_active and u.expires_at
                       and now <= u.expires_at < next_30 and u.plan_type in plans)
        due_30 = db.session.query(func.coalesce(func.sum(SubscriptionInvoice.amount_iqd), 0)).filter(
            SubscriptionInvoice.status == "UNPAID",
            SubscriptionInvoice.due_at >= now,
            SubscriptionInvoice.due_at < next_30).scalar() or 0
        return render_template("admin_billing_dashboard.html", counts={
            "merchants": len(merchants), "clearers": len(clearers),
            "carriers": len(carriers), "total": len(users),
            "active": sum(1 for u in users if u.status_text() == "فعال"),
            "inactive": sum(1 for u in users if u.status_text() != "فعال")
        }, paid=totals("PAID"), unpaid=totals("UNPAID"),
            upcoming=int(due_30), forecast=forecast, prices=prices,
            invoices=invoices, lookup=lookup, plans=plans, labels=labels, users=users,
            billing_csrf=session['billing_csrf'])

    @bp.post("/prices")
    @login_required
    def save_prices():
        admin_only()
        for plan in plans:
            raw = (request.form.get(plan) or "").strip()
            if not raw.isdigit() or int(raw) > 100000000000:
                flash("أدخل سعراً صحيحاً بالدينار العراقي لكل باقة.", "danger")
                return redirect(url_for("billing.dashboard"))
        for plan in plans:
            row = db.session.get(SubscriptionPrice, plan)
            if row is None:
                row = SubscriptionPrice(plan_type=plan)
                db.session.add(row)
            row.amount_iqd = int(request.form[plan])
            row.updated_at = datetime.utcnow()
        db.session.commit()
        flash("تم حفظ أسعار الباقات. الفواتير السابقة لا تتغير.", "success")
        return redirect(url_for("billing.dashboard"))

    @bp.post("/invoices")
    @login_required
    def issue_invoice():
        admin_only()
        try:
            user_id = int(request.form.get("user_id") or 0)
        except ValueError:
            abort(400)
        user = eligible_query().filter(User.id == user_id).first_or_404()
        plan = (request.form.get("plan_type") or "").upper()
        prices = price_map()
        if plan not in plans or plan not in prices or prices[plan] <= 0:
            flash("حدد باقة مدفوعة بسعر معتمد أولاً.", "danger")
            return redirect(url_for("billing.dashboard"))
        # Prevent accidental duplicate open invoices for the same account.
        open_invoice = SubscriptionInvoice.query.filter_by(user_id=user.id, status="UNPAID").first()
        if open_invoice:
            flash("يوجد طلب تسديد مفتوح لهذا الحساب. سوّه أو ألغِه قبل إصدار آخر.", "warning")
            return redirect(url_for("billing.dashboard"))
        db.session.add(SubscriptionInvoice(user_id=user.id, plan_type=plan,
            amount_iqd=prices[plan], due_at=datetime.utcnow()))
        db.session.commit()
        flash("تم إصدار فاتورة غير مسددة؛ لم يتم تفعيل الحساب.", "success")
        return redirect(url_for("billing.dashboard"))

    @bp.post("/invoices/<int:invoice_id>/paid")
    @login_required
    def mark_paid(invoice_id):
        admin_only()
        invoice = db.session.query(SubscriptionInvoice).filter_by(id=invoice_id).with_for_update().first()
        if invoice is None:
            abort(404)
        if invoice.status != "UNPAID":
            flash("هذه الفاتورة تمت معالجتها سابقاً.", "warning")
            return redirect(url_for("billing.dashboard"))
        reference = (request.form.get("payment_reference") or "").strip()
        if not reference or len(reference) > 150:
            flash("مرجع التسديد مطلوب لأغراض التدقيق.", "danger")
            return redirect(url_for("billing.dashboard"))
        user = eligible_query().filter(User.id == invoice.user_id).first_or_404()
        now = datetime.utcnow()
        start = max(now, user.expires_at) if user.expires_at else now
        end = advance(start, invoice.plan_type)
        invoice.status = "PAID"
        invoice.paid_at = now
        invoice.confirmed_by = current_user.id
        invoice.payment_reference = reference
        invoice.period_starts_at = start
        invoice.period_ends_at = end
        user.plan_type = invoice.plan_type
        user.starts_at = start
        user.expires_at = end
        user.license_activated = True
        user.is_active = True
        db.session.commit()
        flash("تم توثيق التسديد وتجديد الحساب دون خسارة الأيام المتبقية.", "success")
        return redirect(url_for("billing.dashboard"))

    @bp.post("/invoices/<int:invoice_id>/void")
    @login_required
    def void_invoice(invoice_id):
        admin_only()
        invoice = db.session.query(SubscriptionInvoice).filter_by(id=invoice_id).with_for_update().first()
        if invoice is None:
            abort(404)
        if invoice.status != "UNPAID":
            flash("لا يمكن إلغاء فاتورة مسددة.", "danger")
            return redirect(url_for("billing.dashboard"))
        invoice.status = "VOID"
        db.session.commit()
        flash("تم إلغاء الفاتورة دون حذف سجلها.", "success")
        return redirect(url_for("billing.dashboard"))

    @app.cli.command("billing-renewals")
    def generate_renewal_invoices():
        """Idempotently issue invoices for customers due within 7 days.
        Only previously paid subscriptions are eligible for auto-invoicing.
        No external payment is attempted, and no access is extended.
        """
        now = datetime.utcnow()
        deadline = now + timedelta(days=7)
        prices = price_map()
        count = 0
        for user in eligible_query().filter(
                User.license_activated.is_(True), User.is_active.is_(True),
                User.expires_at.isnot(None), User.expires_at >= now,
                User.expires_at <= deadline).all():
            if user.plan_type not in plans or prices.get(user.plan_type, 0) <= 0:
                continue
            previously_paid = SubscriptionInvoice.query.filter_by(
                user_id=user.id, status="PAID").first()
            if not previously_paid:
                continue
            already_open = SubscriptionInvoice.query.filter_by(
                user_id=user.id, status="UNPAID").first()
            if already_open:
                continue
            db.session.add(SubscriptionInvoice(
                user_id=user.id, plan_type=user.plan_type,
                amount_iqd=prices[user.plan_type], due_at=user.expires_at))
            count += 1
        db.session.commit()
        print(f"Renewal invoices issued: {count}")

    app.register_blueprint(bp)
    # Deliberately NOT db.create_all() on import: migrations must be applied separately.
    return SubscriptionInvoice, SubscriptionPrice
