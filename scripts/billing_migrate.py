"""Safe, explicit schema setup for PORTEXA billing.
Run only after integrating the billing blueprint into the *local* app.py,
with the service's normal virtualenv and DATABASE_URL. No customer data changes.
"""
import argparse
from sqlalchemy import inspect
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Create only missing billing tables")
    args = parser.parse_args()
    # NOTE: importing legacy app.py runs its existing startup migrations.
    # Always use the production virtualenv and take a verified DB backup first.
    from app import app, db, SubscriptionInvoice, SubscriptionPrice
    with app.app_context():
        engine = db.engine
        names = set(inspect(engine).get_table_names())
        expected = (SubscriptionInvoice.__table__, SubscriptionPrice.__table__)
        for table in expected:
            print(("%s " % table.name) + ("EXISTS" if table.name in names else "MISSING"))
        if not args.apply:
            print("DRY RUN. Use --apply after database backup and staging test.")
            return
        with engine.begin() as connection:
            for table in expected:
                table.create(bind=connection, checkfirst=True)
        print("Billing schema ready. No prices or invoices inserted.")

if __name__ == "__main__":
    main()
