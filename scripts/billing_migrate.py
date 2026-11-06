"""Explicit, guarded billing schema migration for an existing PORTEXA deployment.

Import the app from --app-dir, not the Git checkout containing this script.
A read-only dry run is the default. --apply requires --expected-db.
"""
import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import inspect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", type=Path, default=Path.cwd(),
                        help="Directory containing the deployed app.py")
    parser.add_argument("--expected-db", type=Path,
                        help="Exact SQLite file to verify before writing")
    parser.add_argument("--apply", action="store_true",
                        help="Create only missing billing tables")
    args = parser.parse_args()

    app_dir = args.app_dir.resolve(strict=True)
    if not (app_dir / "app.py").is_file():
        parser.error(f"No app.py found in {app_dir}")
    if args.apply and not args.expected_db:
        parser.error("--apply requires --expected-db")
    if args.expected_db and not args.expected_db.resolve(strict=False).is_file():
        parser.error("Expected database does not exist; refusing to create a new one")

    # This legacy app runs startup setup during import. Use the intended deployment
    # directory and ensure the checkout cannot be imported instead.
    os.chdir(app_dir)
    sys.path.insert(0, str(app_dir))
    from app import app, db, SubscriptionInvoice, SubscriptionPrice

    with app.app_context():
        engine = db.engine
        if engine.dialect.name != "sqlite":
            parser.error("This guarded migration currently supports SQLite only")
        actual = Path(engine.url.database).resolve(strict=False)
        print("Application directory:", app_dir)
        print("Actual database:", actual)
        if args.expected_db:
            expected = args.expected_db.resolve(strict=True)
            if actual != expected:
                parser.error(f"Database mismatch: expected {expected}, got {actual}")
        else:
            print("WARNING: no --expected-db supplied; dry run only")

        names = set(inspect(engine).get_table_names())
        tables = (SubscriptionInvoice.__table__, SubscriptionPrice.__table__)
        for table in tables:
            print(f"{table.name}: {'EXISTS' if table.name in names else 'MISSING'}")
        if not args.apply:
            print("DRY RUN. No billing tables created.")
            return
        with engine.begin() as connection:
            for table in tables:
                table.create(bind=connection, checkfirst=True)
        print("Billing schema ready. No prices or invoices inserted.")


if __name__ == "__main__":
    main()
