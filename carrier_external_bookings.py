"""Carrier-owned external bookings. No changes to legacy Shipment records."""
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
import secrets
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, jsonify
from flask_login import login_required, current_user
from sqlalchemy import UniqueConstraint, CheckConstraint, func

def register_carrier_bookings(app, db, tracking_lookup, notify):
    bp = Blueprint("carrier_bookings", __name__, url_prefix="/carrier/external")

    class Customer(db.Model):
        __tablename__ = "carrier_customer"
        id = db.Column(db.Integer, primary_key=True)
        carrier_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        name = db.Column(db.String(220), nullable=False)
        contact = db.Column(db.String(160))
        phone = db.Column(db.String(80))
        email = db.Column(db.String(200))
        address = db.Column(db.String(400))
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    class Provider(db.Model):
        __tablename__ = "carrier_service_provider"
        id = db.Column(db.Integer, primary_key=True)
        carrier_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        name = db.Column(db.String(220), nullable=False)
        phone = db.Column(db.String(80))
        notes = db.Column(db.Text)

    class Booking(db.Model):
        __tablename__ = "carrier_external_booking"
        id = db.Column(db.Integer, primary_key=True)
        carrier_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        customer_id = db.Column(db.Integer, db.ForeignKey("carrier_customer.id"), nullable=False)
        provider_id = db.Column(db.Integer, db.ForeignKey("carrier_service_provider.id"), nullable=False)
        bl_number = db.Column(db.String(120), nullable=False)
        bl_date = db.Column(db.Date, nullable=False)
        origin_port = db.Column(db.String(200), nullable=False)
        destination_port = db.Column(db.String(200), nullable=False)
        shipping_line = db.Column(db.String(120), nullable=False)
        containers = db.Column(db.Integer, nullable=False)
        container_details = db.Column(db.Text)
        departure_date = db.Column(db.Date)
        arrival_date = db.Column(db.Date)
        due_date = db.Column(db.Date, nullable=False)
        currency = db.Column(db.String(3), nullable=False)
        sale_amount = db.Column(db.Numeric(14, 2), nullable=False)
        provider_cost = db.Column(db.Numeric(14, 2), nullable=False)
        status = db.Column(db.String(30), default="BOOKED", nullable=False)
        notes = db.Column(db.Text)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        customer = db.relationship(Customer)
        provider = db.relationship(Provider)
        __table_args__ = (CheckConstraint("containers > 0"), CheckConstraint("sale_amount >= 0"), CheckConstraint("provider_cost >= 0"),)

    class Payment(db.Model):
        __tablename__ = "carrier_booking_payment"
        id = db.Column(db.Integer, primary_key=True)
        booking_id = db.Column(db.Integer, db.ForeignKey("carrier_external_booking.id"), nullable=False, index=True)
        carrier_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        direction = db.Column(db.String(12), nullable=False)  # CUSTOMER / PROVIDER
        amount = db.Column(db.Numeric(14, 2), nullable=False)
        paid_at = db.Column(db.Date, nullable=False)
        reference = db.Column(db.String(140))
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        __table_args__ = (CheckConstraint("amount > 0"), CheckConstraint("direction IN ('CUSTOMER','PROVIDER')"),)

    class Reminder(db.Model):
        __tablename__ = "carrier_payment_reminder"
        id = db.Column(db.Integer, primary_key=True)
        booking_id = db.Column(db.Integer, db.ForeignKey("carrier_external_booking.id"), nullable=False, index=True)
        direction = db.Column(db.String(12), nullable=False)
        reminder_day = db.Column(db.Date, nullable=False)
        __table_args__ = (UniqueConstraint("booking_id", "direction", "reminder_day", name="uq_carrier_payment_reminder"),)

    def csrf():
        if "carrier_booking_csrf" not in session:
            session["carrier_booking_csrf"] = secrets.token_urlsafe(32)
        return session["carrier_booking_csrf"]

    def guard():
        if not current_user.is_authenticated or not getattr(current_user, "is_carrier", False):
            abort(403)

    def verify():
        if not secrets.compare_digest(request.form.get("_csrf", ""), csrf()):
            abort(400)

    def owned(model, item_id):
        return model.query.filter_by(id=item_id, carrier_id=current_user.id).first_or_404()

    def money(raw):
        try:
            value = Decimal(str(raw)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("المبلغ غير صالح")
        if not value.is_finite() or value < 0 or value > Decimal("999999999999.99"):
            raise ValueError("المبلغ خارج الحدود")
        return value

    def parse_day(raw, required=False):
        if not raw:
            if required:
                raise ValueError("يرجى إدخال جميع التواريخ الإلزامية")
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValueError("صيغة التاريخ غير صحيحة")

    def totals(booking):
        rows = db.session.query(Payment.direction, func.sum(Payment.amount)).filter_by(booking_id=booking.id, carrier_id=current_user.id).group_by(Payment.direction).all()
        paid = dict(rows)
        customer_paid = paid.get("CUSTOMER") or Decimal("0")
        provider_paid = paid.get("PROVIDER") or Decimal("0")
        return {"received": customer_paid, "spent": provider_paid, "receivable": booking.sale_amount-customer_paid, "payable": booking.provider_cost-provider_paid, "profit": booking.sale_amount-booking.provider_cost}

    @bp.before_request
    def check_access():
        guard()

    @bp.get("/")
    @login_required
    def index():
        bookings = Booking.query.filter_by(carrier_id=current_user.id).order_by(Booking.id.desc()).all()
        return render_template("carrier_external.html", view="list", bookings=[(b, totals(b)) for b in bookings], csrf=csrf(), today=date.today())

    @bp.route("/customers", methods=["GET", "POST"])
    @login_required
    def customers():
        if request.method == "POST":
            verify()
            name = request.form.get("name", "").strip()
            if not name or len(name)>220:
                abort(400)
            db.session.add(Customer(carrier_id=current_user.id, name=name, contact=request.form.get("contact", "").strip()[:160], phone=request.form.get("phone", "").strip()[:80], email=request.form.get("email", "").strip()[:200], address=request.form.get("address", "").strip()[:400]))
            db.session.commit()
            flash("تم حفظ الزبون", "success")
            return redirect(url_for(".customers"))
        return render_template("carrier_external.html", view="customers", customers=Customer.query.filter_by(carrier_id=current_user.id).order_by(Customer.name).all(), csrf=csrf())

    @bp.route("/providers", methods=["GET", "POST"])
    @login_required
    def providers():
        if request.method == "POST":
            verify()
            name = request.form.get("name", "").strip()
            if not name or len(name)>220:
                abort(400)
            db.session.add(Provider(carrier_id=current_user.id, name=name, phone=request.form.get("phone", "").strip()[:80], notes=request.form.get("notes", "").strip()[:2000]))
            db.session.commit()
            flash("تم حفظ مقدم الخدمة", "success")
            return redirect(url_for(".providers"))
        return render_template("carrier_external.html", view="providers", providers=Provider.query.filter_by(carrier_id=current_user.id).order_by(Provider.name).all(), csrf=csrf())

    @bp.route("/customers/<int:item_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_customer(item_id):
        item = owned(Customer, item_id)
        if request.method == "POST":
            verify()
            name = request.form.get("name", "").strip()
            if not name or len(name) > 220:
                abort(400)
            item.name = name
            item.contact = request.form.get("contact", "").strip()[:160]
            item.phone = request.form.get("phone", "").strip()[:80]
            item.email = request.form.get("email", "").strip()[:200]
            item.address = request.form.get("address", "").strip()[:400]
            db.session.commit()
            flash("تم تحديث بيانات الزبون", "success")
            return redirect(url_for(".customers"))
        return render_template("carrier_external.html", view="edit_customer", item=item, csrf=csrf())

    @bp.route("/providers/<int:item_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_provider(item_id):
        item = owned(Provider, item_id)
        if request.method == "POST":
            verify()
            name = request.form.get("name", "").strip()
            if not name or len(name) > 220:
                abort(400)
            item.name = name
            item.phone = request.form.get("phone", "").strip()[:80]
            item.notes = request.form.get("notes", "").strip()[:2000]
            db.session.commit()
            flash("تم تحديث بيانات مقدم الخدمة", "success")
            return redirect(url_for(".providers"))
        return render_template("carrier_external.html", view="edit_provider", item=item, csrf=csrf())

    @bp.post("/customers/<int:item_id>/delete")
    @login_required
    def delete_customer(item_id):
        verify()
        item = owned(Customer, item_id)
        if Booking.query.filter_by(carrier_id=current_user.id, customer_id=item.id).first():
            flash("لا يمكن حذف زبون مرتبط بحجوزات؛ احتفظ به لحماية السجل المالي.", "warning")
        else:
            db.session.delete(item)
            db.session.commit()
            flash("تم حذف الزبون", "success")
        return redirect(url_for(".customers"))

    @bp.post("/providers/<int:item_id>/delete")
    @login_required
    def delete_provider(item_id):
        verify()
        item = owned(Provider, item_id)
        if Booking.query.filter_by(carrier_id=current_user.id, provider_id=item.id).first():
            flash("لا يمكن حذف مقدم خدمة مرتبط بحجوزات؛ احتفظ به لحماية السجل المالي.", "warning")
        else:
            db.session.delete(item)
            db.session.commit()
            flash("تم حذف مقدم الخدمة", "success")
        return redirect(url_for(".providers"))

    @bp.route("/new", methods=["GET", "POST"])
    @login_required
    def new():
        if request.method == "POST":
            verify()
            try:
                customer = owned(Customer, int(request.form.get("customer_id", "0")))
                provider = owned(Provider, int(request.form.get("provider_id", "0")))
                bl = request.form.get("bl_number", "").strip()
                origin = request.form.get("origin_port", "").strip()
                destination = request.form.get("destination_port", "").strip()
                line = request.form.get("shipping_line", "").strip()
                count = int(request.form.get("containers", "0"))
                currency = request.form.get("currency", "USD")
                if not all((bl, origin, destination, line)) or count < 1 or count > 10000 or currency not in ("USD", "IQD"):
                    raise ValueError("بيانات الشحنة غير مكتملة")
                if max(map(len, (bl, origin, destination, line))) > 200:
                    raise ValueError("أحد الحقول أطول من المسموح")
                booking = Booking(carrier_id=current_user.id, customer_id=customer.id, provider_id=provider.id,
                    bl_number=bl[:120], bl_date=parse_day(request.form.get("bl_date"), True),
                    origin_port=origin, destination_port=destination, shipping_line=line,
                    containers=count, container_details=request.form.get("container_details", "").strip()[:4000],
                    departure_date=parse_day(request.form.get("departure_date")),
                    arrival_date=parse_day(request.form.get("arrival_date")),
                    due_date=parse_day(request.form.get("due_date"), True), currency=currency,
                    sale_amount=money(request.form.get("sale_amount")),
                    provider_cost=money(request.form.get("provider_cost")),
                    notes=request.form.get("notes", "").strip()[:4000])
                db.session.add(booking)
                db.session.commit()
                flash("تم إنشاء الحجز الخارجي", "success")
                return redirect(url_for(".detail", booking_id=booking.id))
            except (ValueError, TypeError):
                db.session.rollback()
                flash("تعذر الحفظ: تحقق من الحقول والمبالغ والتواريخ", "danger")
        return render_template("carrier_external.html", view="new", customers=Customer.query.filter_by(carrier_id=current_user.id).order_by(Customer.name).all(), providers=Provider.query.filter_by(carrier_id=current_user.id).order_by(Provider.name).all(), csrf=csrf())

    @bp.get("/<int:booking_id>")
    @login_required
    def detail(booking_id):
        booking = owned(Booking, booking_id)
        payments = Payment.query.filter_by(carrier_id=current_user.id, booking_id=booking.id).order_by(Payment.id.desc()).all()
        return render_template("carrier_external.html", view="detail", booking=booking, payments=payments, balance=totals(booking), csrf=csrf())

    @bp.post("/<int:booking_id>/payment")
    @login_required
    def payment(booking_id):
        verify()
        booking = owned(Booking, booking_id)
        direction = request.form.get("direction")
        if direction not in ("CUSTOMER", "PROVIDER"):
            abort(400)
        try:
            amount = money(request.form.get("amount"))
            balance = totals(booking)
            remaining = balance["receivable"] if direction == "CUSTOMER" else balance["payable"]
            if amount <= 0 or amount > remaining:
                raise ValueError("المبلغ يتجاوز المتبقي")
            db.session.add(Payment(booking_id=booking.id, carrier_id=current_user.id, direction=direction, amount=amount, paid_at=parse_day(request.form.get("paid_at"), True), reference=request.form.get("reference", "").strip()[:140]))
            db.session.commit()
            flash("تم تسجيل الدفعة", "success")
        except ValueError:
            db.session.rollback()
            flash("المبلغ أو التاريخ غير صحيح أو يتجاوز الرصيد المتبقي", "danger")
        return redirect(url_for(".detail", booking_id=booking.id))

    @bp.post("/<int:booking_id>/settle")
    @login_required
    def settle(booking_id):
        verify()
        booking = owned(Booking, booking_id)
        direction = request.form.get("direction")
        if direction not in ("CUSTOMER", "PROVIDER"):
            abort(400)
        balance = totals(booking)
        remaining = balance["receivable"] if direction == "CUSTOMER" else balance["payable"]
        if remaining > 0:
            db.session.add(Payment(booking_id=booking.id, carrier_id=current_user.id, direction=direction, amount=remaining, paid_at=date.today(), reference="تسوية كاملة من زر مسددة"))
            db.session.commit()
            flash("تمت التسوية وتسجيل المبلغ المتبقي كدفعة", "success")
        return redirect(url_for(".detail", booking_id=booking.id))

    @bp.get("/<int:booking_id>/tracking")
    @login_required
    def tracking(booking_id):
        booking = owned(Booking, booking_id)
        result = tracking_lookup(bl=booking.bl_number, sealine=booking.shipping_line, force_update=False, route=True)
        return jsonify(result), (200 if result.get("ok") else 502)

    with app.app_context():
        db.create_all()
    app.register_blueprint(bp)
    return bp, Booking, Payment
