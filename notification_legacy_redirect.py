"""Retire the old notification page while preserving historic notification URLs.

Install by calling register_notification_legacy_redirect(app) after existing routes.
The existing endpoints remain valid, but no longer render notifications.html.
"""
from flask import redirect, url_for
from flask_login import current_user, login_required


def register_notification_legacy_redirect(app):
    def home():
        if current_user.is_admin:
            return url_for("billing.dashboard")
        if current_user.is_support:
            return url_for("support_tickets")
        if getattr(current_user, "is_advertiser", False):
            return url_for("advertiser_ads")
        if getattr(current_user, "is_clearer", False):
            return url_for("clearer_dashboard")
        if getattr(current_user, "is_carrier", False):
            return url_for("carrier_dashboard")
        return url_for("dashboard")

    @login_required
    def notifications_list_inline():
        return redirect(home() + "#px-notifications", code=302)

    # Preserve the old endpoint for links already delivered by Web Push.
    app.view_functions["notifications_list"] = notifications_list_inline
