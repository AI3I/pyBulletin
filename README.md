# pyBulletin

A modern packet radio BBS written in Python, compatible with FBB/LinFBB clients and neighbor nodes. Supports Telnet access, AX.25/KISS TNC transport, B1/B2F store-and-forward message exchange, and a web-based sysop console.

---

## Features

- **FBB-compatible command set** — full L/S/R/K family plus RA, RP, LS, LD, LN, LR, LW, NB, NH, MV, ED, NS, ME, BB, WS, DT, X, and more
- **Telnet access** — multi-client asyncio server, configurable idle timeout and rate limits
- **AX.25 / KISS transport** — modulo-8 windowed I-frame connections over serial or TCP KISS TNC (Dire Wolf, soundmodem)
- **B2F forwarding** — outbound and inbound store-and-forward with LZHUF compression; falls back to B1 for older nodes
- **White Pages (WP)** — distributed callbook with NB/NH/NL/NQ/NZ profile commands and WPS search
- **SQLite store** — single-file database with versioned schema migrations, WAL mode, and configurable retention
- **Web sysop console** — token-authenticated dashboard for message management, user admin, and live session monitoring
- **Public web interface** — optional read-only message board (disabled by default)
- **Access control** — per-user privilege levels (user/sysop), per-IP rate limiting, optional login passwords

---

## Requirements

- Python 3.10+
- No mandatory third-party dependencies (pure stdlib + optional `pyserial-asyncio` for hardware TNC)

---

## Installation

```bash
git clone https://github.com/AI3I/pyBulletin.git
cd pyBulletin
python -m pip install -e .
```

For hardware TNC support:

```bash
python -m pip install -e ".[tnc]"
```

For native Bell 202 soundcard receive support:

```bash
python -m pip install -e ".[audio]"
```

---

## Deployment (Linux / systemd)

The `deploy/` directory contains scripts for a production install on any systemd-based Linux distribution (Debian, Ubuntu, Fedora, Raspberry Pi OS, etc.).

```bash
# Install (run as root)
sudo bash deploy/install.sh

# Upgrade to a newer version
sudo bash deploy/upgrade.sh

# Remove completely
sudo bash deploy/uninstall.sh
```

`install.sh` will:
- Create a dedicated `pybulletin` system user and group
- Install the application tree into `/home/pybulletin/pyBulletin`
- Install and enable systemd units:
  - `pybulletin.service` — main BBS (Telnet + web)
  - `pybulletin-forward.service` / `.timer` — scheduled forwarding
  - `pybulletin-retention.service` / `.timer` — nightly message cleanup
- Drop a starter config at `/home/pybulletin/pyBulletin/config/pybulletin.toml`

### Additional deploy scripts

| Script | Purpose |
|--------|---------|
| `deploy/strings.sh` | Push `strings.toml` only — hot-reloads in ≤30s, no restart needed |
| `deploy/setup-nginx.sh` | Configure nginx as reverse proxy for the web interfaces |
| `deploy/doctor.sh` | Diagnose common configuration and permission problems |
| `deploy/migrate.sh` | Run schema migrations on an existing database |
| `deploy/repair.sh` | Attempt to repair a corrupted SQLite database |
| `deploy/fail2ban/` | fail2ban filter and jail for Telnet brute-force protection |
| `deploy/logrotate/` | logrotate config for `/var/log/pybulletin/` |
| `deploy/udev/` | udev rules for C-Media CM108/CM119 HID GPIO access |

### Helper scripts

| Script | Purpose |
|--------|---------|
| `scripts/bootstrap_sysop.py` | Create the initial sysop account |
| `scripts/backup.py` | Hot backup of the SQLite database |
| `scripts/cleanup_retention.py` | Manually trigger message retention cleanup |
| `scripts/migrate_fbb.py` | Import messages from a LinFBB message directory |

---

## Quick Start

```bash
# Copy the example config and edit for your node
cp config/pybulletin.toml config/pybulletin.local.toml
$EDITOR config/pybulletin.local.toml

# Run
pybulletin --config config/pybulletin.local.toml

# Or point at the default location
pybulletin
```

The BBS listens on:

| Service | Default | Config key |
|---------|---------|-----------|
| Telnet | `0.0.0.0:6300` | `[telnet] host / port` |
| Sysop web console | `127.0.0.1:8080` | `[web] host / port` |
| B2F inbound forward | `0.0.0.0:6301` | `[forward] listen_host / listen_port` |
| Public web (opt.) | `127.0.0.1:8081` | `[public_web] host / port` |

---

## Web Interfaces

pyBulletin ships two browser-based interfaces served from the same process.

### Sysop Console (`/sysop`)

A token-authenticated single-page application for node operators. Accessible at `http://127.0.0.1:8080/sysop` by default (bind to loopback and proxy via nginx for external access).

**Capabilities:**
- Message browser — read, hold, release, kill, search across all traffic
- User management — list users, set privilege (user/sysop), reset passwords, delete accounts
- White Pages — look up and edit WP entries
- Neighbor management — view configured forward neighbors and runtime stats
- Node config — edit `[node]`, `[telnet]`, `[forward]`, and `[retention]` settings live
- Statistics dashboard — message counts, connected sessions, forwarding history
- Live session monitor — see who is connected and on which transport

Authentication uses a long-lived bearer token set in `[web] admin_token`. Every request to `/api/*` (except `/api/health`) requires the header `Authorization: Bearer <token>`.

### Public BBS (`/`)

An optional read-only message board for non-amateur visitors. Disabled by default; enable in `[public_web]`.

**Shows:**
- Bulletin and NTS message listings
- Individual message view
- White Pages search

Enable with:

```toml
[public_web]
enabled = true
host    = "127.0.0.1"
port    = 8081
```

Proxy port 8081 through nginx (or any reverse proxy) for public internet access. Personal mail is never exposed through the public interface.

### REST API

Both interfaces are backed by a JSON REST API:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Node health (unauthenticated) |
| `POST` | `/api/auth/login` | Obtain session token |
| `GET` | `/api/messages` | List messages |
| `GET` | `/api/messages/{id}` | Read message |
| `POST` | `/api/messages` | Send message |
| `DELETE` | `/api/messages/{id}` | Kill message |
| `POST` | `/api/messages/{id}/hold` | Hold message |
| `POST` | `/api/messages/{id}/release` | Release held message |
| `GET` | `/api/users` | List users |
| `POST` | `/api/users` | Create user |
| `POST` | `/api/users/{call}/privilege` | Set privilege |
| `POST` | `/api/users/{call}/password` | Reset password |
| `DELETE` | `/api/users/{call}` | Delete user |
| `GET` | `/api/neighbors` | List forward neighbors |
| `GET` | `/api/stats` | Extended node statistics |
| `GET` | `/api/wp` | White Pages lookup |
| `GET/POST` | `/api/config` | Read / update node config |

---

## Configuration

`config/pybulletin.toml` is the main config file. Key sections:

### `[node]`

```toml
[node]
node_call    = "AI3I-1"
owner_name   = "Your Name"
qth          = "FN20"
motd         = "Welcome to AI3I"
hierarchy    = ["WW", "NA", "US", "US-PA"]   # bulletin routing hierarchy
```

### `[telnet]`

```toml
[telnet]
host                 = "0.0.0.0"
port                 = 6300
max_clients          = 50
idle_timeout_seconds = 1800
```

### `[kiss]`

Serial TNC:

```toml
[kiss]
transport = "kiss_serial"
device = "/dev/ttyUSB0"
baud   = 9600
paclen = 236
```

TCP KISS (Dire Wolf, soundmodem):

```toml
[kiss]
transport = "kiss_tcp"
tcp_host = "127.0.0.1"
tcp_port = 8001
```

### `[afsk]`

Direct Bell 202 AFSK modem path for USB soundcard-style interfaces.

```toml
[afsk]
input_device  = ""
output_device = ""
sample_rate   = 48000
mark_hz       = 1200
space_hz      = 2200
baud          = 1200
ptt_device    = ""   # e.g. "serial_rts:/dev/ttyUSB0"
dcd_enabled   = true
```

Select it with `transport = "afsk"` in `[kiss]`.

Current status:
- RX and TX Bell 202 audio paths are implemented for mono 16-bit soundcard I/O via `sounddevice`
- HDLC / AX.25 frame extraction and Bell 202 waveform generation are implemented in-tree
- PTT currently supports no-op operation or serial RTS keying via `ptt_device = "serial_rts:/dev/ttyUSB0"`
- PTT also supports BCM GPIO selectors like `gpio:23` and gpiochip selectors like `gpiochip:/dev/gpiochip0:24`
- PTT also supports CM108/CM119 HID GPIO selectors like `cm108:/dev/hidraw0:3`
- DCD and more robust carrier/symbol recovery are still under development

Common interface patterns:
- Kits4Hams SHARI and similar Pi-mounted nodes: soundcard I/O plus `gpio:<bcm_pin>` or `gpiochip:/dev/gpiochip0:<line>` if PTT is wired to GPIO
- Masters Communications, DMK URI/RIM, and many CM108/119-based USB interfaces: soundcard I/O plus `cm108:/dev/hidrawN:<gpio_pin>`
- modified generic CM108/CM119 USB fobs belong in that same CM108/119 `hidraw` bucket
- Kits4Hams `DINAH` and `PAUL`: scope these with the CM108/CM119 USB-interface family, not the SHARI embedded-radio family
- Kits4Hams `BRIAN`: scope this with integrated radio/interface boards like SHARI, not with the CM108/119 interface family
- SignaLink-class USB interfaces: soundcard I/O plus VOX, no-op PTT, or `serial_rts:/dev/ttyUSB0` when a serial control port is available
- AllStar ARI / URI / RIM style interfaces: soundcard I/O plus whichever PTT path the interface exposes, typically CM108 HID, GPIO, or serial RTS

Full hardware matrix and setup notes:
- [Hardware Guide](/home/jdlewis/GitHub/pyBulletin/docs/hardware.md)

Examples:

```toml
[kiss]
transport = "afsk"

[afsk]
input_device  = "hw:1,0"
output_device = "hw:1,0"
sample_rate   = 48000
mark_hz       = 1200
space_hz      = 2200
baud          = 1200

# C-Media CM108/119 GPIO 3 on /dev/hidraw0
ptt_device    = "cm108:/dev/hidraw0:3"

# Raspberry Pi BCM GPIO 23
# ptt_device  = "gpio:23"

# USB serial RTS
# ptt_device  = "serial_rts:/dev/ttyUSB0"
```

Diagnostics:

```bash
pybulletin --config config/pybulletin.local.toml doctor-afsk
```

Deployment notes:
- `deploy/install.sh` now adds the service user to the `audio` group for USB soundcard and `hidraw` access
- `deploy/install.sh` installs a C-Media udev rule so CM108/CM119 `hidraw` devices are group-writable by `audio`
- `deploy/doctor.sh` reports the selected AX.25 transport plus AFSK audio/PTT setup status

### `[forward]`

```toml
[forward]
enabled      = true
listen_port  = 6301

[[forward.neighbor]]
call       = "AI3I-1"
address    = "w1bbs.example.net:6301"
protocol   = "b2"
schedule   = "0 */4 * * *"   # UTC cron — every 4 hours
categories = ["WW", "NA", "US"]
enabled    = true
```

### `[retention]`

```toml
[retention]
personal_mail_days = 30
bulletin_days      = 14
nts_days           = 7
killed_days        = 1
```

### `[web]` — Sysop console

```toml
[web]
host        = "127.0.0.1"
port        = 8080
admin_token = "change-me-to-a-long-random-string"
```

Generate a token with `python -c "import secrets; print(secrets.token_hex(32))"`.

### `[public_web]` — Public message board

```toml
[public_web]
enabled = true
host    = "127.0.0.1"
port    = 8081
```

---

## Command Reference

### Reading

| Command | Description |
|---------|-------------|
| `L` / `LA` | List messages since your message base |
| `LL [n]` | List last *n* messages (default 20) |
| `LM` / `LP` | List your personal mail |
| `LB [cat]` | List bulletins (optionally filtered by category) |
| `LT` | List NTS traffic |
| `LH` | List held messages |
| `LK` | List killed messages |
| `LF` | List forwarded messages |
| `LY` | List read messages |
| `LW` | List worldwide (WW) bulletins |
| `LN` | List messages new since last login |
| `LR [n]` | List last *n* messages in reverse order |
| `LS <text>` | Search by subject, To:, or From: |
| `LD <date>` | List messages since date (MMDD, YYMMDD, or YYYYMMDD) |
| `L> n` | List from message number *n* |
| `R n [n …]` | Read message(s) |
| `RA` | Read all new personal mail sequentially |
| `N` | Show new message summary; advance message base |

### Sending

| Command | Description |
|---------|-------------|
| `S [call]` / `SP [call]` | Send personal mail |
| `SB [cat]` | Send bulletin |
| `ST [call]` / `SN [call]` | Send NTS traffic |
| `SC n call[@bbs]` | Copy message to another recipient |
| `RP n` | Reply to message *n* (pre-fills To: and Re: subject) |

End a message with `/EX` or `Ctrl-Z`. Abort with `/AB`.

### Message Management

| Command | Description |
|---------|-------------|
| `K n [n …]` / `D` / `RM` | Kill (delete) message(s) you own |
| `K> call` / `K< call` / `K@ bbs` | Kill all to / from / at a callsign |
| `KM` | Kill all your personal mail |

### Profile / Options

| Command | Description |
|---------|-------------|
| `NH [name]` | Set display name |
| `NL [grid]` | Set Maidenhead locator (e.g. `FN20`) |
| `NQ [city]` | Set city / QTH description |
| `NZ [zip]` | Set ZIP / postal code |
| `NB [call]` | Set home BBS callsign |
| `O` | Show current options |
| `O LINES n` | Set pager line count (0 = off) |
| `O COLS n` | Set terminal column width |
| `O EXPERT ON\|OFF` | Toggle expert mode (suppress hints) |
| `O PW` | Change password |
| `X` | Toggle expert mode (shorthand) |

### Info / Status

| Command | Description |
|---------|-------------|
| `I [call]` / `P [call]` | Node info or White Pages lookup |
| `WS <name>` | Search White Pages by name or partial callsign |
| `W` | Show connected sessions |
| `ME` | Show your callsign, privilege, and profile |
| `DT` | Current UTC date and time |
| `NS` | Node statistics |
| `BB` | List configured neighbor nodes |
| `H` / `?` | Help (`??` for full, `?X` for command detail) |
| `V` | Version info |
| `G` / `GB` / `GE` / `Q` | Disconnect |

### Sysop Commands

| Command | Description |
|---------|-------------|
| `SH n [n …]` / `MH` | Hold message(s) |
| `SR n [n …]` / `MR` | Release held message(s) |
| `ED n` | Interactively edit subject and body of message *n* |
| `MV n call[@bbs]` | Reassign message to a different recipient / BBS |
| `F [neighbor]` | Trigger outbound forward (all neighbors or one) |
| `U [search]` | List users |

---

## Forwarding

pyBulletin supports both **outbound** (caller) and **inbound** (called) B2F forwarding sessions.

**Outbound** connects are triggered by the cron schedule defined per neighbor, or manually with the `F` command.

**Inbound** connections are accepted on `[forward] listen_port` (default 6301). The remote node must connect with a B2F SID handshake.

### Tested Interoperability

| Software | Protocol |
|----------|----------|
| LinFBB (F6FBB) | B2F |
| BPQ32 (G8BPQ) | B2F |
| JNOS | B2F |
| Winlink / Airmail | B2F |
| Kantronics KPC-3 PBBS | B1 (falls back automatically) |

---

## AX.25 / KISS

With a KISS TNC configured, pyBulletin accepts direct AX.25 connections in addition to Telnet. The node callsign (`node_call`) is used as the destination address. Digipeater paths are not required but are supported.

Tested with:
- **Dire Wolf** (software TNC) via TCP KISS
- **Kantronics KPC-3** and **KPC-9612** via serial KISS
- **soundmodem** via TCP KISS

Direct Bell 202 AFSK support is being added natively.  The current tree
contains configuration plumbing and HDLC framing helpers, but not yet the
finished soundcard modem/DSP implementation.

---

## Development

```bash
# Install dev dependencies
python -m pip install -e ".[dev]"

# Run tests
python -m pytest

# Run a single test file
python -m pytest tests/test_store.py -v
```

Test files:

| File | Covers |
|------|--------|
| `tests/test_store.py` | SQLite store — messages, users, WP, prefs |
| `tests/test_ax25.py` | AX.25 framing, KISS, connection state machine |
| `tests/test_forward.py` | SID parsing, B1/B2F proposal and message encoding |
| `tests/test_engine.py` | Command dispatch via `FakeSession` |

---

## Project Structure

```
src/pybulletin/
  cli.py            — entry point, asyncio server setup
  config.py         — TOML config loader / dataclasses
  strings.py        — localizable string catalog
  access_policy.py  — capability / privilege checks
  session/          — BBSSession: login, prompt loop, paging
  command/          — CommandEngine: all BBS commands
  store/            — BBSStore: SQLite, models, migrations
  forward/          — B2F forwarding: SID, protocol, scheduler
  ax25/             — AX.25 framing, KISS TNC transport
  transport/        — abstract transport layer
  web/              — aiohttp sysop console + public web
  address.py        — hierarchical bulletin address matching
vendor/
  lzhuf.py          — pure-Python LZHUF compression (B2F)
config/
  pybulletin.toml   — example configuration
  strings.toml      — message strings (localizable)
```

---

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).
