# Architecture decisions and change log

## 2026-11-09 — Separate Operations subdomain

Decision: proposed `operations.portexa.urrisetech.com`, with its own Nginx HTTPS virtual host proxying to `127.0.0.1:8767`. Keep the main PORTEXA Flask service on `127.0.0.1:5001`. No direct public exposure of Gunicorn. Deployment pending DNS, ingress and security verification.

## 2026-11-09 — Keep original shipment and manual route storage separate

Original shipment edits belong to PORTEXA `shipment` data; manual port and voyage updates belong to `portexa_manual_routes` in Tracking Engine. Source DB write access stays disabled until explicitly authorized.

## 2026-11-09 — Employee shipment review UX

Feature branch `feature/employee-shipment-edit` contains employee shipment edits, route forms, ETD/ETA date-time inputs, and a filter by presence of bill-of-lading number. Verify deployed version and tests independently; documentation is not proof of deployment.

## 2026-11-09 — Distinct public and outbound IPs

PORTEXA DNS resolves to `104.153.151.6`, while the Ubuntu host outbound request reported `5.62.149.8`. These are not interchangeable. Exact provider ingress topology remains to be verified.
