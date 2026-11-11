"""One-time, idempotent SQLite migration for Factory.country_id.

Run on a BACKUP copy first. Never guess a country for a factory used in
shipments with different country IDs. Those factories remain unassigned
until their owner selects a country in the new management form.
Usage: python scripts/migrate_factory_country.py /path/to/app.db
"""
import sqlite3
import sys
from pathlib import Path

def migrate(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        raise SystemExit(f"Database not found: {path}")
    with sqlite3.connect(str(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(factory)")}
        if not cols:
            raise SystemExit("factory table missing")
        if "country_id" not in cols:
            conn.execute("ALTER TABLE factory ADD COLUMN country_id INTEGER REFERENCES country(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_factory_country_id ON factory(country_id)")
        # Infer only where all existing shipments of a factory agree on one country.
        conn.execute("""
            UPDATE factory SET country_id = (
                SELECT MIN(s.country_id) FROM shipment s
                WHERE s.factory_id = factory.id
            )
            WHERE country_id IS NULL
              AND (SELECT COUNT(DISTINCT s.country_id) FROM shipment s
                   WHERE s.factory_id = factory.id) = 1
        """)
        unassigned = conn.execute("SELECT COUNT(*) FROM factory WHERE country_id IS NULL").fetchone()[0]
        conflicts = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT factory_id FROM shipment
                GROUP BY factory_id HAVING COUNT(DISTINCT country_id) > 1
            )
        """).fetchone()[0]
        print(f"Migration complete. Unassigned shippers: {unassigned}. Conflicting historic shippers: {conflicts}.")
        print("Assign missing countries via /manage/factories before creating shipments with those shippers.")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/migrate_factory_country.py /path/to/app.db")
    migrate(sys.argv[1])
