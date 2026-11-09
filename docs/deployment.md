# Deployment and validation runbook

## Safe read-only checks

```bash
systemctl is-active portexa-operations.service
curl -s -o /dev/null -w 'HTTP: %{http_code}\n' http://127.0.0.1:8767/login
sudo nginx -t
sudo certbot certificates
sudo ufw status
dig +short portexa.urrisetech.com A
dig +short operations.portexa.urrisetech.com A
```

## Updating Operations feature branch

```bash
cd /home/alialameeri/Portexa-Tracking-Engine
git status --short
git pull --ff-only origin feature/employee-shipment-edit
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src/portexa_tracking
```

Only if tests pass and a deployment is approved:

```bash
sudo systemctl restart portexa-operations.service
systemctl is-active portexa-operations.service
curl -s -o /dev/null -w 'HTTP: %{http_code}\n' http://127.0.0.1:8767/login
sudo journalctl -u portexa-operations.service --since '2 minutes ago' --no-pager -p err
```

## Public hostname rollout checklist

- [ ] Verify external ingress / provider forwarding topology.
- [ ] Confirm DNS target and provider controls.
- [ ] Audit app authentication, session secret persistence, cookie policy, rate limiting, proxy headers and CSRF.
- [ ] Back up Nginx config and prepare isolated Operations server block.
- [ ] Validate `nginx -t` before reload.
- [ ] Obtain a certificate for the exact Operations hostname.
- [ ] Test HTTPS login and permissions externally without opening port 8767.
- [ ] Add main PORTEXA navigation link for authorized roles.
- [ ] Verify monitoring, backups and rollback procedure.

No production changes are performed by adding these documentation files.
