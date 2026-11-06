#!/usr/bin/env python3
"""Safely install public landing and Google onboarding into a deployed PORTEXA.

Dry run by default. Never replace server app.py or touch database/env/service.
"""
import argparse
import ast
import difflib
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("public_onboarding.py", "templates/public_landing.html")
REGISTRATION = (
    "\n# Public landing and Google trial onboarding.\n"
    "from public_onboarding import register_public_onboarding\n"
    "register_public_onboarding(app, db, User, generate_license_key, plan_to_expiry)\n\n"
)
ANCHOR = 'if __name__ == "__main__":'


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, type=Path)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    server = args.server.resolve(strict=True)
    app_file = server / "app.py"
    if not app_file.is_file():
        p.error("Missing production app.py")
    for rel in FILES:
        if not (ROOT / rel).is_file():
            p.error(f"Missing checkout file: {rel}")
    old = app_file.read_text(encoding="utf-8")
    if REGISTRATION.strip() in old:
        new = old
    else:
        if old.count(ANCHOR) != 1:
            p.error("Could not find unique app startup anchor")
        new = old.replace(ANCHOR, REGISTRATION + ANCHOR, 1)
    ast.parse(new)
    ast.parse((ROOT / "public_onboarding.py").read_text(encoding="utf-8"))
    print("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                      fromfile="server/app.py", tofile="proposed/app.py"))
          or "Registration already present")
    print("Files:", ", ".join(FILES))
    if not args.apply:
        print("DRY RUN: no changes.")
        return
    backup = Path.home() / "portexa-backups" / (
        "public-onboarding-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(parents=True, exist_ok=False)
    for rel in ("app.py",) + FILES:
        path = server / rel
        if path.is_file():
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    for rel in FILES:
        dest = server / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    if new != old:
        tmp = server / ".app.py.onboarding.tmp"
        tmp.write_text(new, encoding="utf-8")
        tmp.chmod(app_file.stat().st_mode & 0o777)
        tmp.replace(app_file)
    print("Applied. Backup:", backup)
    print("Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI securely in service env before enabling signup.")


if __name__ == "__main__":
    main()
