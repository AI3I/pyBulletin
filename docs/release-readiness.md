# Release Readiness

This document separates what is currently ship-ready from what still needs
field validation or future development.

## Fit To Ship

These areas are ready for normal VPS or LAN deployment:

- Core BBS service under `pybulletin.service`
- Web/sysop/API service under `pybulletinweb.service`
- Telnet user access
- SQLite store and retention timer
- B1/B2F forwarding helpers
- KISS serial and KISS TCP configuration paths
- Dire Wolf / soundmodem style TCP KISS integration
- fail2ban filters for core and web authentication failures
- install, upgrade, repair, uninstall, doctor, and nginx helper scripts
- optional nginx admin allowlist/basic-auth generation
- static web UI smoke checks for referenced IDs and assets
- SYSOP bootstrap login flow

## Recommended First RF Path

Use an external modem/TNC first:

- `kiss_tcp` with Dire Wolf or soundmodem
- `kiss_serial` with a hardware KISS TNC

For Raspberry Pi 3B+ and Kits4Hams SHARI Pi3V, start with:

- [Raspberry Pi 3B+ + SHARI Pi3V + Dire Wolf](hardware/direwolf-shari-pi.md)

## Field Validation Needed

These need real hardware time before they should be called production-proven:

- Raspberry Pi 3B+ boot persistence with SHARI Pi3V / SA818
- Dire Wolf audio device naming and service startup order on Raspberry Pi OS
- SHARI TX deviation and RX audio level tuning
- RF end-to-end connect, login, message read/send, and disconnect
- RF store-and-forward behavior against a known neighbor
- recovery after audio device disappearance or Dire Wolf restart

## Native AFSK Status

The native `afsk` transport is implemented enough for lab testing:

- Bell 202 RX/TX audio path
- HDLC / AX.25 framing
- serial RTS, GPIO, gpiochip, and CM108/CM119 PTT selectors
- logical channel/port labeling for native AFSK heard-station/router state
- deployment and doctor checks

It still needs:

- stronger DCD / carrier handling
- better noisy-channel symbol recovery
- real interface-specific audio tuning notes
- longer unattended tests on ARM hardware

Until that work is done, Dire Wolf remains the recommended path for first-time
RF users.

## Web Exposure Notes

`pybulletinweb.service` defaults to `127.0.0.1:8080`. Keep it on loopback and
proxy it with nginx for internet access.

The public UI and sysop console share the same backend:

- `/` is the public UI when `[public_web] enabled = true`
- `/sysop` is the sysop console
- `/api/health` is unauthenticated
- other `/api/*` paths require an authenticated session

The built-in sysop authentication is required, but internet-facing nodes can
also enable nginx controls in `deploy/setup-nginx.sh`:

- `--admin-allow CIDR` for trusted admin networks
- `--admin-basic-auth FILE` for HTTP basic authentication
- a VPN or SSH tunnel for sysop access
- a separate private hostname for operator access

The generated nginx controls protect `/sysop`, `/api/`, and `/ws`, while leaving
`/api/health` open for monitoring. Use them for operator-only deployments or a
private admin hostname. Be careful on public web nodes: public browser workflows
may also use API routes, so coarse `/api/` restrictions are intentionally
conservative.

## Future Development

Good next engineering targets:

- native AFSK noisy-channel hardening
- PACTOR BBS session bridging after SCS hardware validation
- packaged release notes and version tags for each public ship point
