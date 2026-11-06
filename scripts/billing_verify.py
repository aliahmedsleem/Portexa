"""Read-only checks for PORTEXA billing after migration.
Does not create invoices, update subscriptions, or change prices.
"""
from sqlalchemy import func, inspect

def main():
    from app import app, db, User, SubscriptionInvoice, SubscriptionPrice
    with app.app_context():
        names = set(inspect(db.engine).get_table_names())
        required = {"subscription_invoice", "subscription_price"}
        missing = sorted(required - names)
        if missing:
            raise SystemExit("Missing tables: " + ", ".join(missing))
        clients = User.query.filter(
            User.is_admin.is_(False),
            User.is_support.is_(False),
            User.is_advertiser.is_(False),
            User.username.notin_(("admin", "systemadmin"))
        )
        print("Clients:", clients.count())
        print("Merchants:", clients.filter(User.is_clearer.is_(False), User.is_carrier.is_(False)).count())
        print("Clearers:", clients.filter(User.is_clearer.is_(True)).count())
        print("Carriers:", clients.filter(User.is_carrier.is_(True)).count())
        for status in ("UNPAID", "PAID", "VOID"):
            rows = SubscriptionInvoice.query.filter_by(status=status)
            amount = db.session.query(func.coalesce(func.sum(SubscriptionInvoice.amount_iqd), 0)).filter(
                SubscriptionInvoice.status == status).scalar()
            print(status, "count:", rows.count(), "IQD:", int(amount or 0))
        duplicates = db.session.query(
            SubscriptionInvoice.user_id, func.count(SubscriptionInvoice.id)
        ).filter(SubscriptionInvoice.status == "UNPAID").group_by(
            SubscriptionInvoice.user_id
        ).having(func.count(SubscriptionInvoice.id) > 1).all()
        if duplicates:
            raise SystemExit("ERROR: duplicate unpaid invoices found")
        print("Open invoice uniqueness: OK")
        print("Prices configured:", SubscriptionPrice.query.count())
        print("READ-ONLY CHECK COMPLETE")

if __name__ == "__main__":
    main()
