"""Opt-in Web Push campaigns for PORTEXA. Requires pywebpush and VAPID environment variables."""
import json
import os
import secrets
from datetime import datetime
from urllib.parse import urlparse
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for, Response
from flask_login import current_user, login_required
from sqlalchemy import or_

def register_push_campaigns(app, db, User, SystemNotification):
    bp = Blueprint("push_campaigns", __name__, url_prefix="/push")
    class Campaign(db.Model):
        __tablename__ = "push_campaign"
        id = db.Column(db.Integer, primary_key=True)
        advertiser_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
        creator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
        kind = db.Column(db.String(12), nullable=False)  # SYSTEM / AD
        audience = db.Column(db.String(12), nullable=False)  # ALL / MERCHANT / CLEARER / CARRIER
        title = db.Column(db.String(160), nullable=False)
        body = db.Column(db.String(500), nullable=False)
        target_url = db.Column(db.String(500), nullable=False, default="/notifications")
        scheduled_at = db.Column(db.DateTime, nullable=False)
        status = db.Column(db.String(20), nullable=False, default="PENDING")
        amount = db.Column(db.Numeric(14, 2), nullable=True)
        currency = db.Column(db.String(3), nullable=True)
        reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
        reviewed_at = db.Column(db.DateTime)
        rejection_reason = db.Column(db.String(500))
        sent_count = db.Column(db.Integer, default=0, nullable=False)
        failed_count = db.Column(db.Integer, default=0, nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    class Subscription(db.Model):
        __tablename__ = "push_subscription"
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        endpoint = db.Column(db.Text, nullable=False, unique=True)
        payload = db.Column(db.Text, nullable=False)
        ads_enabled = db.Column(db.Boolean, nullable=False, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    class Delivery(db.Model):
        __tablename__ = "push_campaign_delivery"
        id = db.Column(db.Integer, primary_key=True)
        campaign_id = db.Column(db.Integer, db.ForeignKey("push_campaign.id"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
        subscription_id = db.Column(db.Integer, db.ForeignKey("push_subscription.id"), nullable=False)
        status = db.Column(db.String(12), nullable=False)
        __table_args__ = (db.UniqueConstraint("campaign_id", "subscription_id", name="uq_push_campaign_subscription"),)

    with app.app_context():
        db.create_all()

    def token():
        if "push_csrf" not in session:
            session["push_csrf"] = secrets.token_urlsafe(32)
        return session["push_csrf"]

    def verify():
        value = request.headers.get("X-Push-CSRF") or request.form.get("_csrf", "")
        if not secrets.compare_digest(value, token()):
            abort(400)

    def is_admin():
        return bool(getattr(current_user, "is_admin", False))

    def is_advertiser():
        return bool(getattr(current_user, "is_advertiser", False))

    def safe_url(value):
        value = (value or "/notifications").strip()
        parsed = urlparse(value)
        if not value.startswith("/") or value.startswith("//") or parsed.netloc or parsed.scheme or "\\" in value:
            raise ValueError("الرابط يجب أن يكون مساراً داخلياً")
        return value[:500]

    def audience_users(c):
        q = User.query.filter_by(is_active=True)
        if c.audience == "MERCHANT":
            q = q.filter(User.is_admin.is_(False), User.is_advertiser.is_(False), User.is_carrier.is_(False), User.is_clearer.is_(False), User.is_support.is_(False))
        elif c.audience == "CARRIER":
            q = q.filter(User.is_carrier.is_(True))
        elif c.audience == "CLEARER":
            q = q.filter(User.is_clearer.is_(True))
        else:
            q = q.filter(User.is_admin.is_(False), User.is_advertiser.is_(False), User.is_support.is_(False))
        return q

    @bp.get("/sw.js")
    def worker():
        js = """self.addEventListener('push',event=>{let d={};try{d=event.data.json()}catch(e){};event.waitUntil(self.registration.showNotification(d.title||'PORTEXA',{body:d.body||'',tag:d.tag||'portexa',data:{url:d.url||'/notifications'}}))});self.addEventListener('notificationclick',event=>{event.notification.close();const u=new URL(event.notification.data.url,self.location.origin);if(u.origin!==self.location.origin)return;event.waitUntil(clients.openWindow(u.href))});"""
        return Response(js, mimetype="application/javascript", headers={"Service-Worker-Allowed": "/" ,"Cache-Control": "no-store"})

    @bp.get("/config")
    @login_required
    def config():
        return jsonify(publicKey=os.environ.get("PUSH_VAPID_PUBLIC_KEY", ""), csrf=token())

    @bp.post("/subscribe")
    @login_required
    def subscribe():
        verify()
        data = request.get_json(silent=True) or {}
        obj = data.get("subscription") or {}
        endpoint = obj.get("endpoint", "")
        keys = obj.get("keys") or {}
        if not endpoint.startswith("https://") or len(endpoint) > 2048 or not all(keys.get(k) for k in ("p256dh", "auth")):
            abort(400)
        record = Subscription.query.filter_by(endpoint=endpoint).first()
        if record is None:
            record = Subscription(endpoint=endpoint, user_id=current_user.id)
            db.session.add(record)
        record.user_id = current_user.id
        record.payload = json.dumps(obj)
        record.ads_enabled = bool(data.get("ads_enabled", False))
        db.session.commit()
        return jsonify(ok=True)

    @bp.post("/unsubscribe")
    @login_required
    def unsubscribe():
        verify()
        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint", "")
        Subscription.query.filter_by(user_id=current_user.id, endpoint=endpoint).delete()
        db.session.commit()
        return jsonify(ok=True)

    @bp.route("/campaigns", methods=["GET", "POST"])
    @login_required
    def campaigns():
        if not (is_admin() or is_advertiser()):
            abort(403)
        if request.method == "POST":
            verify()
            kind = (request.form.get("kind") or "AD").upper()
            audience = (request.form.get("audience") or "").upper()
            if audience not in ("ALL", "MERCHANT", "CLEARER", "CARRIER"):
                abort(400)
            if not is_admin() and kind != "AD":
                abort(403)
            if kind not in ("AD", "SYSTEM"):
                abort(400)
            advertiser_id = None
            if kind == "AD":
                advertiser_id = int(request.form.get("advertiser_id") or (current_user.id if is_advertiser() and not is_admin() else 0))
                advertiser = User.query.filter_by(id=advertiser_id, is_advertiser=True).first_or_404()
                if not is_admin() and advertiser.id != current_user.id:
                    abort(403)
            title = (request.form.get("title") or "").strip()
            body = (request.form.get("body") or "").strip()
            if not title or not body or len(title)>160 or len(body)>500:
                abort(400)
            try:
                when = datetime.fromisoformat(request.form.get("scheduled_at", ""))
                if when < datetime.utcnow().replace(second=0, microsecond=0):
                    raise ValueError("موعد الحملة يجب أن يكون مستقبلياً")
                target = safe_url(request.form.get("target_url"))
            except (ValueError, TypeError) as exc:
                flash(str(exc), "danger")
                return redirect(url_for(".campaigns"))
            db.session.add(Campaign(advertiser_id=advertiser_id, creator_id=current_user.id, kind=kind, audience=audience, title=title, body=body, target_url=target, scheduled_at=when, status="APPROVED" if kind=="SYSTEM" else "PENDING"))
            db.session.commit()
            flash("تم حفظ الخطة؛ الإعلانات تنتظر موافقة المدير.", "success")
            return redirect(url_for(".campaigns"))
        q = Campaign.query if is_admin() else Campaign.query.filter_by(advertiser_id=current_user.id)
        return render_template("push_campaigns.html", campaigns=q.order_by(Campaign.id.desc()).limit(150).all(), advertisers=User.query.filter_by(is_advertiser=True).all() if is_admin() else [], admin=is_admin(), csrf=token())

    @bp.post("/campaigns/<int:campaign_id>/review")
    @login_required
    def review(campaign_id):
        if not is_admin():
            abort(403)
        verify()
        c = Campaign.query.get_or_404(campaign_id)
        if c.status != "PENDING":
            abort(409)
        action = request.form.get("action")
        if action not in ("approve", "reject"):
            abort(400)
        c.status = "APPROVED" if action == "approve" else "REJECTED"
        c.reviewer_id = current_user.id
        c.reviewed_at = datetime.utcnow()
        c.rejection_reason = (request.form.get("reason") or "").strip()[:500] if action == "reject" else None
        db.session.commit()
        return redirect(url_for(".campaigns"))

    @bp.post("/campaigns/<int:campaign_id>/cancel")
    @login_required
    def cancel(campaign_id):
        verify()
        c = Campaign.query.get_or_404(campaign_id)
        if not (is_admin() or c.creator_id == current_user.id):
            abort(403)
        if c.status not in ("PENDING", "APPROVED"):
            abort(409)
        c.status = "CANCELLED"
        db.session.commit()
        return redirect(url_for(".campaigns"))

    def dispatch_due():
        """Call from a single scheduled process. Delivery is idempotent per device."""
        from pywebpush import webpush, WebPushException
        key = os.environ.get("PUSH_VAPID_PRIVATE_KEY")
        subject = os.environ.get("PUSH_VAPID_SUBJECT")
        if not key or not subject:
            raise RuntimeError("Configure PUSH_VAPID_PRIVATE_KEY and PUSH_VAPID_SUBJECT")
        for c in Campaign.query.filter(Campaign.status=="APPROVED", Campaign.scheduled_at<=datetime.utcnow()).order_by(Campaign.id).all():
            c.status = "SENDING"
            db.session.commit()
            users = audience_users(c).with_entities(User.id).all()
            for (user_id,) in users:
                subs = Subscription.query.filter_by(user_id=user_id, ads_enabled=(c.kind=="AD")).all() if c.kind=="AD" else Subscription.query.filter_by(user_id=user_id).all()
                for sub in subs:
                    if Delivery.query.filter_by(campaign_id=c.id, subscription_id=sub.id).first():
                        continue
                    db.session.add(Delivery(campaign_id=c.id, user_id=user_id, subscription_id=sub.id, status="ATTEMPTED"))
                    db.session.commit()
                    try:
                        webpush(subscription_info=json.loads(sub.payload), data=json.dumps({"title":("إعلان ممول: " if c.kind=="AD" else "")+c.title,"body":c.body,"url":c.target_url,"tag":"campaign-"+str(c.id)}), vapid_private_key=key, vapid_claims={"sub":subject}, timeout=12)
                        c.sent_count += 1
                        Delivery.query.filter_by(campaign_id=c.id, subscription_id=sub.id).update({"status":"SENT"})
                    except WebPushException as exc:
                        c.failed_count += 1
                        Delivery.query.filter_by(campaign_id=c.id, subscription_id=sub.id).update({"status":"FAILED"})
                        if getattr(getattr(exc,"response",None),"status_code",None) in (404,410):
                            db.session.delete(sub)
                    db.session.commit()
            c.status = "COMPLETED"
            db.session.commit()

    @app.cli.command("push-dispatch")
    def dispatch_command():
        dispatch_due()

    app.register_blueprint(bp)
    return Campaign, Subscription
