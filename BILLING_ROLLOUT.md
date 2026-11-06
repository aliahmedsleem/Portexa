# PORTEXA billing rollout (staging first)

## Current architecture
- `subscription_billing.py` adds two independent tables: `subscription_invoice` and `subscription_price`.
- `templates/admin_billing_dashboard.html` shows merchant, clearer and carrier counts, excluding system administrator, support, advertiser and reserved bootstrap usernames.
- Money is stored as integer IQD; paid and unpaid amounts are based on invoices, not account activation flags.
- Confirming payment is manual and requires an audit reference. It renews the account through the existing `User` fields.
- Existing `User.ensure_valid()` and login/guard checks deny access after expiration. **This does not run a timed job to flip flags at midnight**; denial occurs on the next request/login.
- Renewal CLI: `flask --app app billing-renewals`, intended to run daily after prices and migrations are verified. Only accounts with previously paid invoices and expiration within seven days receive a renewal invoice; duplicates are avoided while an unpaid invoice exists.
- A generated invoice does NOT mean payment and does NOT extend access.

## Important production warning
`/opt/portexa/app.py` has local server changes not in GitHub. NEVER overwrite it from the branch. Integrate only the blueprint registration, dashboard redirect and navigation changes into a staging copy of the local version after reviewing its diff.

## Integration snippet for local app.py
Insert just BEFORE the final `if __name__ == "__main__":` block:

```python
from subscription_billing import register_billing
SubscriptionInvoice, SubscriptionPrice = register_billing(app, db, User)
```

Change admin login redirect and `admin_home` to `url_for("billing.dashboard")` only after the blueprint is registered. Install `subscription_billing.py`, `templates/admin_billing_dashboard.html`, and the updated `templates/admin_base.html`.

## One-time database migration
After taking a verified database backup and integrating the module:

```bash
python - <<'PY'
from app import app, db, SubscriptionInvoice, SubscriptionPrice
with app.app_context():
    SubscriptionInvoice.__table__.create(bind=db.engine, checkfirst=True)
    SubscriptionPrice.__table__.create(bind=db.engine, checkfirst=True)
    print("Billing tables ready")
PY
```

Use the same Python virtualenv, DATABASE_URL and environment variables as the running service. Test on a copy of the database first.

## Tests before enabling billing
1. Verify admin access only; normal user and support user must get HTTP 403 on `/admin/billing/`.
2. Verify merchants, clearers, carriers and exclusions against real users.
3. Save prices, create one unpaid invoice and verify it does not extend access.
4. Mark paid with a unique reference and verify expiration moves by a calendar month/year without losing remaining days.
5. Re-submit paid confirmation: must not charge or renew again.
6. Void unpaid invoice and verify it is not counted as debt.
7. Expire a test user, verify login and authenticated requests are denied.
8. Run `flask --app app billing-renewals` twice and confirm only one unpaid renewal invoice exists.
9. Check backups and rollback procedures.

## Still required before full production billing
- Reconcile historical subscriptions and opening balances; never invent past paid amounts.
- Add explicit billing status/grace policy and separate administrative suspension from overdue payment.
- Decide tax, discount, currency, partial payments, refunds and receipt numbering rules.
- Add immutable payment-event ledger and database-level idempotency for concurrent workers.
- Add payment notifications/reminders (email/SMS/WhatsApp) only with consent and a configured provider.
- Add scheduler (systemd timer) and monitoring only after staging verification.
- Remove or rotate default bootstrap passwords (`admin123`) securely.
- Existing local bootstrap code resets privileged account activation/expiry on startup; audit and fix separately.
- Enforce CSRF protection on existing legacy admin forms too.


## Updated explicit migration and read-only verification

After copying `subscription_billing.py` and integrating its registration into the server-local `app.py`, **do not copy the repository app.py over the server-local version**.

Use the same Python interpreter and environment variables as the production service:

```bash
# On a staging database first; do not point to production before review.
python scripts/billing_migrate.py
python scripts/billing_migrate.py --apply
python scripts/billing_verify.py
```

Before a production migration, take a consistent SQLite backup using the SQLite backup API (or a database-native PostgreSQL backup), not a blind copy of a live database. Review the app's effective `DATABASE_URL` and service environment first. Never place credentials or payment references in public logs.

**Do not schedule `billing-renewals` yet.** Existing manual activation and deactivation paths remain available in the legacy admin user editor, so a fully unified subscription state machine and administrative suspension state are still required. The new finance module is a controlled pilot, not a completed automatic billing service.
