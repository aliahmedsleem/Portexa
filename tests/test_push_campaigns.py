import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from urllib.parse import urlsplit

from flask import Flask
from flask_login import LoginManager, UserMixin
from flask_sqlalchemy import SQLAlchemy
from jinja2 import ChoiceLoader, DictLoader
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from push_campaigns import register_push_campaigns


class CampaignFixture(unittest.TestCase):
    def setUp(self):
        self.configure_app("sqlite:///:memory:")

    def configure_app(self, database_uri):
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        app.config.update(
            TESTING=True,
            SECRET_KEY="isolated-test-key",
            SQLALCHEMY_DATABASE_URI=database_uri,
        )
        layouts = {
            name: '<html dir="rtl">{% block content %}{% endblock %}</html>'
            for name in ("admin_base.html", "advertiser_base.html")
        }
        app.jinja_loader = ChoiceLoader([DictLoader(layouts), app.jinja_loader])
        self.app = app
        self.db = db = SQLAlchemy(app)
        login = LoginManager(app)
        login.login_view = "login"
        app.add_url_rule("/login", "login", lambda: "Sign in")

        with app.app_context():
            @event.listens_for(db.engine, "connect")
            def enable_foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys = ON")

        class User(db.Model, UserMixin):
            id = db.Column(db.Integer, primary_key=True)
            is_admin = db.Column(db.Boolean, default=False)
            is_advertiser = db.Column(db.Boolean, default=False)
            username = db.Column(db.String, default="Test user")
            company_name = db.Column(db.String)

        @login.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        self.User = User
        self.Campaign, self.Subscription = register_push_campaigns(app, db, User, None)
        self.delivery = db.metadata.tables["push_campaign_delivery"]
        self.client = app.test_client()
        with app.app_context():
            self.addCleanup(self.dispose, db.engine)
            self.assertEqual(db.session.execute(text("PRAGMA foreign_keys")).scalar_one(), 1)
            db.session.add_all([User(id=1, is_admin=True), User(id=2, is_advertiser=True), User(id=3)])
            db.session.flush()
            db.session.add(self.Subscription(
                id=1, user_id=3, endpoint="https://example.invalid/push", payload="{}"
            ))
            db.session.commit()

    @staticmethod
    def dispose(engine):
        engine.dispose()

    def sign_in(self, user_id):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
        return self.client.get("/push/config").get_json()["csrf"]

    def campaign(self, status):
        with self.app.app_context():
            campaign = self.Campaign(
                advertiser_id=2, creator_id=2, kind="AD", audience="ALL",
                title="Test campaign", body="Test notification",
                scheduled_at=datetime(2030, 1, 1), status=status,
            )
            self.db.session.add(campaign)
            self.db.session.flush()
            campaign_id = campaign.id
            self.db.session.execute(self.delivery.insert().values(
                campaign_id=campaign_id, user_id=3, subscription_id=1, status="SENT"
            ))
            self.db.session.commit()
            return campaign_id

    @staticmethod
    def delete_url(campaign_id):
        return f"/push/campaigns/{campaign_id}/delete"

    def delete(self, campaign_id, csrf=None, *, confirmed=True):
        data = {"confirm_delete": "yes"} if confirmed else {}
        if csrf is not None:
            data["_csrf"] = csrf
        return self.client.post(self.delete_url(campaign_id), data=data)

    def assert_history(self, campaign_id, *, present):
        with self.app.app_context():
            self.assertEqual(self.db.session.get(self.Campaign, campaign_id) is not None, present)
            deliveries = self.db.session.execute(self.delivery.select().where(
                self.delivery.c.campaign_id == campaign_id
            )).all()
            self.assertEqual(len(deliveries), int(present))


class CampaignDeletionTests(CampaignFixture):
    def test_signed_out_redirects_to_login(self):
        campaign_id = self.campaign("REJECTED")
        response = self.delete(campaign_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.location).path, "/login")
        self.assert_history(campaign_id, present=True)

    def test_non_admins_are_denied_even_with_valid_csrf(self):
        campaign_id = self.campaign("CANCELLED")
        for user_id in (2, 3):
            with self.subTest(user_id=user_id):
                self.assertEqual(self.delete(campaign_id, self.sign_in(user_id)).status_code, 403)
                self.assert_history(campaign_id, present=True)

    def test_delete_requires_post_csrf_and_confirmation(self):
        csrf = self.sign_in(1)
        campaign_id = self.campaign("COMPLETED")
        self.assertEqual(self.client.get(self.delete_url(campaign_id)).status_code, 405)
        self.assertEqual(self.delete(campaign_id).status_code, 400)
        self.assertEqual(self.delete(campaign_id, "invalid").status_code, 400)
        self.assertEqual(self.delete(campaign_id, "غير صالح").status_code, 400)
        self.assertEqual(self.delete(campaign_id, csrf, confirmed=False).status_code, 400)
        self.assert_history(campaign_id, present=True)

    def test_active_and_unknown_statuses_are_protected(self):
        csrf = self.sign_in(1)
        for status in ("PENDING", "APPROVED", "SENDING", "UNKNOWN"):
            with self.subTest(status=status):
                campaign_id = self.campaign(status)
                self.assertEqual(self.delete(campaign_id, csrf).status_code, 409)
                self.assert_history(campaign_id, present=True)

    def test_only_selected_terminal_history_and_its_deliveries_are_deleted(self):
        csrf = self.sign_in(1)
        other_id = self.campaign("COMPLETED")
        for status in ("COMPLETED", "CANCELLED", "REJECTED"):
            with self.subTest(status=status):
                campaign_id = self.campaign(status)
                response = self.delete(campaign_id, csrf)
                self.assertEqual((response.status_code, response.location), (302, "/push/campaigns"))
                self.assert_history(campaign_id, present=False)
                self.assertEqual(self.delete(campaign_id, csrf).status_code, 404)
        self.assert_history(other_id, present=True)
        with self.app.app_context():
            self.assertEqual(self.User.query.count(), 3)
            self.assertIsNotNone(self.db.session.get(self.Subscription, 1))

    def test_parent_delete_failure_rolls_back_delivery_cleanup(self):
        csrf = self.sign_in(1)
        campaign_id = self.campaign("COMPLETED")
        with self.app.app_context():
            self.db.session.execute(text(
                "CREATE TRIGGER reject_campaign_delete BEFORE DELETE ON push_campaign "
                "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
            ))
            self.db.session.commit()
        with self.assertRaises(IntegrityError):
            self.delete(campaign_id, csrf)
        self.assert_history(campaign_id, present=True)

    def test_page_only_offers_confirmation_to_admin_for_terminal_history(self):
        terminal = self.campaign("COMPLETED")
        pending = self.campaign("PENDING")
        self.sign_in(1)
        page = self.client.get("/push/campaigns").get_data(as_text=True)
        self.assertIn(self.delete_url(terminal), page)
        self.assertIn('name="confirm_delete"', page)
        self.assertNotIn(self.delete_url(pending), page)
        self.sign_in(2)
        page = self.client.get("/push/campaigns").get_data(as_text=True)
        self.assertIn("Test campaign", page)
        self.assertNotIn('name="confirm_delete"', page)


class CampaignConcurrencyTests(CampaignFixture):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.configure_app(f"sqlite:///{Path(folder) / 'campaigns.sqlite'}")

    def test_stale_cancel_cannot_make_sending_campaign_deletable(self):
        campaign_id = self.campaign("APPROVED")
        csrf = self.sign_in(1)
        with self.app.app_context():
            engine = self.db.engine

        waiting, resume = Event(), Event()

        @event.listens_for(engine, "before_cursor_execute")
        def pause_cancellation(connection, cursor, statement, parameters, context, many):
            if statement.startswith("UPDATE push_campaign") and "CANCELLED" in parameters:
                waiting.set()
                if not resume.wait(5):
                    raise TimeoutError("Waiting for the competing status update")

        with ThreadPoolExecutor(max_workers=1) as pool:
            cancellation = pool.submit(
                self.client.post, f"/push/campaigns/{campaign_id}/cancel", data={"_csrf": csrf}
            )
            try:
                self.assertTrue(waiting.wait(5))
                with engine.begin() as connection:
                    changed = connection.execute(
                        self.Campaign.__table__.update()
                        .where(self.Campaign.id == campaign_id, self.Campaign.status == "APPROVED")
                        .values(status="SENDING")
                    )
                    self.assertEqual(changed.rowcount, 1)
            finally:
                resume.set()
            self.assertEqual(cancellation.result().status_code, 409)

        self.assertEqual(self.delete(campaign_id, csrf).status_code, 409)
        self.assert_history(campaign_id, present=True)
