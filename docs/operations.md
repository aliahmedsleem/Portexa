# PORTEXA Operations portal

## Current application

- Repository: `aliahmedsleem/Portexa-Tracking-Engine`.
- Development branch: `feature/employee-shipment-edit`; separate from this documentation branch in the main PORTEXA repository.
- Ubuntu checkout: `/home/alialameeri/Portexa-Tracking-Engine`.
- systemd: `portexa-operations.service`; Gunicorn `127.0.0.1:8767`.
- Environment file: `/etc/portexa-operations/operations.env` (never commit or print its secrets).
- `PORTEXA_OPS_HTTPS=1` set in systemd unit; confirm persistent session secret, trusted proxy handling, secure cookies, login rate limits and role enforcement before public launch.
- Existing LAN Nginx TLS endpoint: `192.168.100.10:8766`.
- Desired hostname: `operations.portexa.urrisetech.com` (not yet deployed).
- Current employee interface has assigned tasks, shipment editing, manual route editing, review submission, and a filter for shipments with/without a bill of lading number.
- Route date fields ETD/ETA changed to datetime-local inputs on feature branch. Validate browser rendering and save flow.

## Data ownership and safety

- Production PORTEXA Flask repository: `aliahmedsleem/Portexa`, Ubuntu deployment `/opt/portexa`, database `/opt/portexa/app.db`.
- Tracking Engine database commonly at `/home/alialameeri/Portexa-Tracking-Engine/data/tracking.sqlite3` (verify runtime environment override).
- Original shipment records live in PORTEXA table `shipment`.
- Manual route fields live in Tracking Engine table `portexa_manual_routes` keyed by source shipment ID.
- Main PORTEXA can read manual route data from the Tracking Engine DB; ensure both services resolve the same DB path.
- **Do not enable `PORTEXA_SOURCE_WRITE_ENABLED=YES` without explicit approval, tested backup/restore, staging validation and role/security review.**
- Employee login is currently separate from main PORTEXA login; single sign-on is a future project, not implemented.

## Integration target

1. Public Nginx HTTPS virtual host for Operations, proxying only to loopback 8767.
2. Navigation entry in main PORTEXA to the Operations portal for authorized staff.
3. Centralized identity / SSO only after a security and account-mapping design review.
4. Never assume separate applications share cookies or user accounts.
