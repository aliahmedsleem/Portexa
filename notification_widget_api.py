"""Inline notification read-state API. No database migrations required."""
import hmac
import secrets

from flask import Blueprint, abort, jsonify, request, session
from flask_login import current_user, login_required


def register_notification_widget_api(app, db, SystemNotification):
    bp = Blueprint("notification_widget_api", __name__, url_prefix="/notifications/widget")

    @app.context_processor
    def widget_token_context():
        def token():
            if not current_user.is_authenticated:
                return ""
            if "_px_notification_token" not in session:
                session["_px_notification_token"] = secrets.token_urlsafe(32)
            return session["_px_notification_token"]
        return {"px_notification_token": token}

    @bp.post("/<int:notification_id>/read")
    @login_required
    def mark_read(notification_id):
        expected = session.get("_px_notification_token", "")
        supplied = request.headers.get("X-Notification-CSRF", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(403)
        notification = SystemNotification.query.filter_by(
            id=notification_id, user_id=current_user.id
        ).first_or_404()
        if not notification.is_read:
            notification.is_read = True
            db.session.commit()
        unread = SystemNotification.query.filter_by(
            user_id=current_user.id, is_read=False
        ).count()
        return jsonify(ok=True, unread=unread)

    app.register_blueprint(bp)
