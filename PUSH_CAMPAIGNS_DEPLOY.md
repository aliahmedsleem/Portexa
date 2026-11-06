# PORTEXA Web Push campaigns (initial release)

Files: push_campaigns.py, templates/push_campaigns.html, templates/notifications.html, modified app.py and navigation templates.

## Safety before deploying
The production /opt/portexa directory is NOT a Git repository and its app.py may differ from the GitHub branch. **Never replace production app.py wholesale.** Back up app.py, the database and changed templates. Copy the new push_campaigns.py and push_campaigns.html and updated notification/sidebar templates only after comparing them. Apply these app.py changes manually to production:
- before the existing carrier external bookings registration near the bottom:
  `from push_campaigns import register_push_campaigns`
  `register_push_campaigns(app, db, User, SystemNotification)`
- in guard_everything(), add `or request.path.startswith("/push")` to the allow-list for admin, advertiser, carrier, clearer and support. Regular merchants are already allowed non-restricted paths.

Install dependency inside the production venv: `/opt/portexa/.venv/bin/python -m pip install 'pywebpush>=2.0,<3'`.

## VAPID keys
Generate once with `/opt/portexa/.venv/bin/vapid --gen` (consult installed py-vapid CLI help; CLI flags differ by release), then configure environment variables:
- PUSH_VAPID_PUBLIC_KEY: base64url encoded uncompressed P-256 public key
- PUSH_VAPID_PRIVATE_KEY: corresponding PEM private key or accepted pywebpush key
- PUSH_VAPID_SUBJECT: mailto:admin@example.com (use your actual support email)

Keep the private key outside Git and available to the systemd service and the scheduled worker. Confirm `GET /push/config` returns the public key for an authenticated user.

## Sending due campaigns
The web service DOES NOT send campaigns automatically on approval; configure exactly one systemd timer or cron job to invoke:
`cd /opt/portexa && /opt/portexa/.venv/bin/flask --app app push-dispatch`
For safety, first test with a small campaign and one opted-in browser. The dispatcher records attempts per device to avoid re-sending; a crash between recording an attempt and sending may result in a missed notification, which must be investigated manually.

## User consent
Users enable Web Push on the notifications page and explicitly check a separate opt-in for advertising. Browser support and permission are required. The Service Worker URL is `/push/sw.js` with scope `/push/`. A user may revoke a device subscription in the same UI.

## Roles and pricing
- Advertiser: creates AD campaigns only, sees their own.
- Admin: creates free SYSTEM campaigns, creates AD campaigns on behalf of a selected advertiser, approves or rejects pending ads and assigns a price/currency on approval.
- Audiences: ALL, MERCHANT, CLEARER, CARRIER.
- Scheduled times are UTC. A campaign executes only when approved and due.

## Current limitations
This is a first-stage release. The ad price is recorded but **no invoice, collection or payment confirmation** is implemented. Approval is a manual business decision; do not approve an unpaid campaign if payment is required. There is no separate administrator approval step for free SYSTEM campaigns. Delivery counts represent webpush API accepted calls, not guaranteed device display. No click analytics yet. No automatic notification mirroring into the existing SystemNotification table. No campaign throttling or quota enforcement yet. Consider these before commercial launch.
