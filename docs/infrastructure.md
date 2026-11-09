# Infrastructure and network

## Observed topology (2026-11-09)

- Ubuntu server LAN address: `192.168.100.10/24`; interface `enp0s31f6`.
- Default route: `192.168.100.1`.
- Additional interfaces: `wg0` (`10.77.64.6/32`) and `tailscale0` (`100.96.207.113/32`).
- Main PORTEXA public DNS A record: `portexa.urrisetech.com -> 104.153.151.6`.
- Server outbound public address as seen by api.ipify.org: `5.62.149.8`.
- The public/static IP service and web hosting are distinct services. **Do not equate the outbound IP with the inbound DNS endpoint.**
- Main domain `https://portexa.urrisetech.com` responded 302 to `/welcome`, then 200, with Nginx response headers on 2026-11-09. This does not alone establish the exact inbound forwarding mechanism.
- Proposed Operations domain: `operations.portexa.urrisetech.com`; no DNS A answer at last check.

## Reverse proxies and services

| Service | Public / local hostname | Upstream | Notes |
|---|---|---|---|
| PORTEXA Flask | portexa.urrisetech.com | 127.0.0.1:5001 | Nginx TLS reverse proxy |
| PORTEXA Operations | proposed operations.portexa.urrisetech.com | 127.0.0.1:8767 | Gunicorn 2 workers; NOT publicly deployed on new hostname |
| Operations LAN proxy | 192.168.100.10:8766 | 127.0.0.1:8767 | Nginx LAN TLS with local certificate |
| Iraqi Business | business.urrisetech.com | 127.0.0.1:8001 | Nginx TLS reverse proxy |
| URRISE console | console.urrisetech.com | 127.0.0.1:8000 | Nginx TLS reverse proxy |
| URRISE website | urrisetech.com / www | 127.0.0.1:5050 | Nginx TLS reverse proxy |

Nginx sites-enabled also includes `default`, `portexa`, `portexa-public`, `portexa-operations-lan`, `iraqi-business`, `urrise-console`, `urrisetech.com`. Check actual loaded config before editing.

## Firewall observations

UFW active. TCP 80/443 allowed publicly; SSH 22 and TCP 8081 allowed from 192.168.100.0/24; TCP 8766 bound to 192.168.100.10 allowed from 192.168.100.0/24. Do not expose Gunicorn 8767.

## TLS

Certbot certificates observed for `portexa.urrisetech.com`, `business.urrisetech.com`, `console.urrisetech.com`, `urrisetech.com` and `www.urrisetech.com`. No certificate yet observed for `operations.portexa.urrisetech.com`.

## Pending verification

1. Confirm how static IP 104.153.151.6 reaches this home Ubuntu server (provider forwarding / tunnel / reverse proxy).
2. Determine whether the same ingress can serve a second TLS hostname on TCP 443.
3. Create DNS record only after confirming correct ingress and ownership.
4. Validate external HTTP-01 or DNS-01 certificate issuance path without disrupting the existing main site.


## WireGuard observation (2026-11-09)

- Interface `wg0` active; reported a recent handshake (30 seconds at capture time), persistent keepalive 25 seconds, and nonzero bidirectional transfer.
- Peer endpoint: `23.156.200.196:1194`. Peer AllowedIPs includes `0.0.0.0/0` and `::/0`; interface fwmark `0xca6c`.
- `ip route get 104.153.151.6` returned `via 192.168.100.1 dev enp0s31f6 src 192.168.100.10`. This describes outbound routing to that address, **not** the inbound forwarding path for the public domain.
- WireGuard connectivity alone does not prove that `104.153.151.6` is routed or forwarded to the server. Check policy routing and ingress/provider forwarding separately.
- Public keys are not authentication secrets, but avoid including them unnecessarily in published documentation; private keys must never be committed.


## DNS administration (user-provided configuration, 2026-11-09)

- Authoritative DNS records are managed in Squarespace Domains for `urrisetech.com` (account DNS settings).
- User-reported A records: apex (`@`), `portexa`, `business`, `console`, and `www` all point to `104.153.151.6`.
- Proposed new A record: name `operations.portexa`, value `104.153.151.6`, TTL 5 minutes if supported. This yields `operations.portexa.urrisetech.com`.
- This is a proposed change, **not yet verified as saved or propagated**. Do not alter existing A, MX, SPF or DKIM records.
- DNS pointing to the public address does not itself establish HTTP/TLS ingress forwarding or a working Nginx virtual host.
