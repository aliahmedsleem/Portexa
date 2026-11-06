#!/usr/bin/env python3
"""Deploy one-click license activation without overwriting production app.py.

Default: dry run. --apply: back up app.py and template, then patch atomically.
No database operations, service restart, or changes to other server files.
"""
import argparse
import ast
import difflib
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "templates/admin_user_form.html"

OLD_CREATE = '''        flash("تم إنشاء المستخدم بنجاح." if activate_now else "تم إنشاء المستخدم. فعّل الحساب حتى يتمكن من الدخول.", "success")
        return redirect(url_for("admin_users"))'''
NEW_CREATE = '''        flash("تم إنشاء المستخدم وتوليد اللايسنس تلقائياً. يمكنك تفعيله الآن." if not activate_now else "تم إنشاء المستخدم وتفعيل اللايسنس.", "success")
        return redirect(url_for("admin_users_edit", user_id=user.id))'''

OLD_ACTIVATE = '''            code = (request.form.get("license_code") or "").strip()
            if code and code == (user.license_key or ""):
                user.license_activated = True
                user.is_active = True
                user.starts_at = datetime.utcnow()
                user.expires_at = plan_to_expiry(user.plan_type)
                flash("تم تفعيل الحساب بنجاح.", "success")
            else:
                flash("كود اللايسنس غير صحيح.", "danger")'''
NEW_ACTIVATE = '''            if user.is_admin:
                flash("لا يمكن تعديل تفعيل حساب المدير من هذه الشاشة.", "danger")
            elif user.license_activated and user.is_active:
                flash("اللايسنس مفعل مسبقاً. لا حاجة لإعادة التفعيل.", "info")
            else:
                user.license_key = user.license_key or generate_license_key()
                user.license_activated = True
                user.is_active = True
                user.starts_at = datetime.utcnow()
                user.expires_at = plan_to_expiry(user.plan_type)
                flash("تم تفعيل اللايسنس تلقائياً وبدأت مدة الاشتراك.", "success")'''

def change_once(content, old, new, label):
    if new in content:
        print(f"{label}: already applied")
        return content
    if content.count(old) != 1:
        raise RuntimeError(f"{label}: expected one exact legacy block, found {content.count(old)}. No files changed.")
    return content.replace(old, new, 1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    server = args.server.resolve(strict=True)
    app_path = server / "app.py"
    tpl_path = server / TEMPLATE
    source_tpl = ROOT / TEMPLATE
    for p in (app_path, tpl_path, source_tpl):
        if not p.is_file():
            parser.error(f"Missing file: {p}")
    old = app_path.read_text(encoding="utf-8")
    new = change_once(old, OLD_CREATE, NEW_CREATE, "create redirect")
    new = change_once(new, OLD_ACTIVATE, NEW_ACTIVATE, "one-click activation")
    ast.parse(new)
    print("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                     fromfile="server/app.py", tofile="proposed/app.py"))
          or "No app.py changes needed")
    print("Template to install:", source_tpl)
    if not args.apply:
        print("DRY RUN: no files changed.")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path.home() / "portexa-backups" / f"license-activation-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    for path, rel in ((app_path, "app.py"), (tpl_path, TEMPLATE)):
        dest = backup / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    if old != new:
        temp = server / ".app.py.license.tmp"
        temp.write_text(new, encoding="utf-8")
        temp.chmod(app_path.stat().st_mode & 0o777)
        temp.replace(app_path)
    shutil.copy2(source_tpl, tpl_path)
    print("Applied. Backup:", backup)
    print("Validate syntax, restart service, and test create/activate flow.")

if __name__ == "__main__":
    main()
