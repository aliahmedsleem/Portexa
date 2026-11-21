"""PORTEXA system notification Web Push bridge.

Commands:
  flask push-events-init       # baseline existing notifications, no historical blast
  flask push-events-dispatch   # deliver pending events; run from ONE scheduled worker
No push is sent during HTTP requests. Ads consent is handled by campaign code.
"""
import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy import text
from pywebpush import webpush, WebPushException


def register_push_events(app, db, SystemNotification):
    @app.cli.command("push-events-init")
    def init_events():
        with app.app_context():
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS push_event_state (
                    id INTEGER PRIMARY KEY,
                    last_notification_id INTEGER NOT NULL
                )
            """))
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS push_event_delivery (
                    notification_id INTEGER NOT NULL,
                    subscription_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    PRIMARY KEY (notification_id, subscription_id)
                )
            """))
            db.session.execute(text("""
                INSERT INTO push_event_state(id, last_notification_id)
                SELECT 1, COALESCE(MAX(id), 0) FROM system_notification
                WHERE NOT EXISTS (SELECT 1 FROM push_event_state WHERE id = 1)
            """))
            db.session.commit()
            print("Push events initialized; old notifications will not be broadcast.")

    @app.cli.command("push-events-dispatch")
    def dispatch_events():
        with app.app_context():
            key = os.environ.get("PUSH_VAPID_PRIVATE_KEY")
            subject = os.environ.get("PUSH_VAPID_SUBJECT")
            if not key or not subject:
                raise RuntimeError("VAPID environment is missing")
            state = db.session.execute(text(
                "SELECT last_notification_id FROM push_event_state WHERE id = 1"
            )).scalar()
            if state is None:
                raise RuntimeError("Run flask push-events-init first")

            # Queue new notifications, one delivery per device, then advance cursor.
            # Existing events without subscribers are intentionally not replayed.
            events = SystemNotification.query.filter(
                SystemNotification.id > state
            ).order_by(SystemNotification.id).limit(250).all()
            for n in events:
                subscriptions = db.session.execute(text(
                    "SELECT id FROM push_subscription WHERE user_id = :uid"
                ), {"uid": n.user_id}).all()
                for (sub_id,) in subscriptions:
                    db.session.execute(text("""
                        INSERT OR IGNORE INTO push_event_delivery
                        (notification_id, subscription_id, status, attempts)
                        VALUES (:nid, :sid, 'PENDING', 0)
                    """), {"nid": n.id, "sid": sub_id})
                db.session.execute(text(
                    "UPDATE push_event_state SET last_notification_id = :nid WHERE id = 1"
                ), {"nid": n.id})
            db.session.commit()

            pending = db.session.execute(text("""
                SELECT d.notification_id, d.subscription_id, d.attempts,
                       n.title, n.body, n.url, s.payload
                FROM push_event_delivery d
                JOIN system_notification n ON n.id = d.notification_id
                JOIN push_subscription s ON s.id = d.subscription_id
                    AND s.user_id = n.user_id
                WHERE d.status = 'PENDING'
                  AND (d.next_retry_at IS NULL OR d.next_retry_at <= :now)
                ORDER BY d.notification_id, d.subscription_id
                LIMIT 100
            """), {"now": datetime.utcnow().isoformat()}).all()

            for nid, sid, attempts, title, body, url, payload in pending:
                # Restrict notification click navigation to internal paths.
                parsed = urlparse(url or "")
                target = url if url and url.startswith("/") and not url.startswith("//") and not parsed.netloc and not parsed.scheme and "\\\\" not in url else "/notifications"
                data = json.dumps({
                    "title": title or "PORTEXA",
                    "body": body or "",
                    "url": target,
                    "tag": "event-" + str(nid),
                }, ensure_ascii=False)
                status = "SENT"
                next_retry = None
                try:
                    webpush(subscription_info=json.loads(payload), data=data,
                            vapid_private_key=key, vapid_claims={"sub": subject},
                            timeout=12)
                except WebPushException as exc:
                    http_status = getattr(getattr(exc, "response", None), "status_code", None)
                    if http_status in (404, 410):
                        status = "EXPIRED"
                        db.session.execute(text(
                            "DELETE FROM push_subscription WHERE id = :sid"
                        ), {"sid": sid})
                    elif attempts >= 2:
                        status = "FAILED"
                    else:
                        status = "PENDING"
                        next_retry = (datetime.utcnow() + timedelta(minutes=5 * (attempts + 1))).isoformat()
                except Exception:
                    if attempts >= 2:
                        status = "FAILED"
                    else:
                        status = "PENDING"
                        next_retry = (datetime.utcnow() + timedelta(minutes=5 * (attempts + 1))).isoformat()
                    current_app.logger.exception("Push event delivery error nid=%s sid=%s", nid, sid)
                db.session.execute(text("""
                    UPDATE push_event_delivery
                    SET status = :status, attempts = attempts + 1,
                        next_retry_at = :next_retry
                    WHERE notification_id = :nid AND subscription_id = :sid
                """), {"status": status, "next_retry": next_retry,
                       "nid": nid, "sid": sid})
                db.session.commit()
            print("New notifications queued:", len(events),
                  "delivery attempts:", len(pending))
