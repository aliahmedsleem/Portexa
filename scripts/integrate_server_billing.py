#!/usr/bin/env python3
"""Integrate billing into the existing production app.py without replacing it.

Run from the repository checkout:
 python3 scripts/integrate_server_billing.py --server /opt/portexa
 python3 scripts/integrate_server_billing.py --server /opt/portexa --apply

This tool only edits app.py and copies the two billing files. It never touches
the database, systemd service, environment file, or billing schedules.
"""
import argparse
import ast
import difflib
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRATION = (
    "# Subscription billing: separate module, preserving server-local routes.\n"
    "from subscription_billing import register_billing\n"
    "SubscriptionInvoice, SubscriptionPrice = register_billing(app, db, User)\n\n"
)
NEEDED = (
    "subscription_billing.py",
    "templates/admin_billing_dashboard.html",
    "templates/admin_base.html",
)

def replace_once(source, old, new, label):
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match; found {count}; refusing to edit")
    return source.replace(old, new, 1)

def patch(source):
    if "SubscriptionInvoice, SubscriptionPrice = register_billing(app, db, User)" not in source:
        anchor = 'if __name__ == "__main__":'
        source = replace_once(source, anchor, REGISTRATION + anchor, "billing registration")
    # Keep existing routes and local patches. Redirect only the two admin entry points.
    old = 'def admin_home():\n    return redirect(url_for("admin_users"))'
    if old in source:
        source = replace_once(source, old,
            'def admin_home():\n    return redirect(url_for("billing.dashboard"))', "admin home")
    old = '        if user.is_admin:\n            return redirect(url_for("admin_users"))'
    if old in source:
        source = replace_once(source, old,
            '        if user.is_admin:\n            return redirect(url_for("billing.dashboard"))', "admin login")
    ast.parse(source)
    return source

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    target = args.server.resolve()
    source_app = target / "app.py"
    if not source_app.is_file():
        raise SystemExit("Server app.py not found")
    for name in NEEDED:
        if not (ROOT / name).is_file():
            raise SystemExit(f"Missing checkout file: {name}")
    old = source_app.read_text(encoding="utf-8")
    new = patch(old)
    print("Server app.py:", source_app)
    print("Change preview:")
    print("".join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile="server/app.py", tofile="server/app.py (proposed)"
    )) or "(registration and redirects already present)")
    print("Will copy:", ", ".join(NEEDED))
    if not args.apply:
        print("DRY RUN ONLY. No files changed. Use --apply after review.")
        return
    backup_dir = Path.home() / "portexa-backups" / (
        "billing-files-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    for name in ("app.py",) + NEEDED:
        existing = target / name
        if existing.is_file():
            destination = backup_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(existing, destination)
    print("File backups:", backup_dir)
    for name in NEEDED:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    if new != old:
        temp = target / ".app.py.billing.tmp"
        temp.write_text(new, encoding="utf-8")
        temp.chmod(source_app.stat().st_mode & 0o777)
        temp.replace(source_app)
    print("Files staged; NO database migration and NO service restart performed.")
    print("Next: verify syntax, back up database, run billing_migrate.py --apply, then restart.")

if __name__ == "__main__":
    main()
