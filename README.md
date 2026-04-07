# pyBulletin

A modern packet radio BBS written in Python, compatible with FBB/LinFBB clients and neighbor nodes. Supports Telnet access, AX.25/KISS TNC transport, B1/B2F store-and-forward message exchange, and a web-based sysop console.

---

## Features

- **FBB-compatible command set** — full L/S/R/K family plus RA, RP, LS, LD, LN, LR, LW, NB, NH, MOVE, ED, STATS, WHOAMI, BBS, WPS, DATE, X, and more
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

## Configuration

`config/pybulletin.toml` is the main config file. Key sections:

### `[node]`

```toml
[node]
node_call    = "W3BBS-1"
owner_name   = "Your Name"
qth          = "FN20"
motd         = "Welcome to W3BBS"
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
device = "/dev/ttyUSB0"
baud   = 9600
paclen = 236
```

TCP KISS (Dire Wolf, soundmodem):

```toml
[kiss]
tcp_host = "127.0.0.1"
tcp_port = 8001
```

### `[forward]`

```toml
[forward]
enabled      = true
listen_port  = 6301

[[forward.neighbor]]
call       = "W1BBS-1"
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

### `[web]`

```toml
[web]
host        = "127.0.0.1"
port        = 8080
admin_token = "change-me-to-a-long-random-string"
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
| `K n [n …]` / `D` / `KILL` / `RM` | Kill (delete) message(s) you own |
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
| `WPS <name>` | Search White Pages by name or partial callsign |
| `W` / `WHO` | Show connected sessions |
| `WHOAMI` | Show your callsign, privilege, and profile |
| `DATE` / `TIME` | Current UTC date and time |
| `STATS` | Node statistics |
| `BBS` | List configured neighbor nodes |
| `H` / `?` | Help |
| `V` | Version info |
| `G` / `GB` / `GE` / `B` / `BYE` / `Q` | Disconnect |

### Sysop Commands

| Command | Description |
|---------|-------------|
| `SH n [n …]` / `MH` | Hold message(s) |
| `SR n [n …]` / `MR` | Release held message(s) |
| `ED n` | Interactively edit subject and body of message *n* |
| `MOVE n call[@bbs]` | Reassign message to a different recipient / BBS |
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

MIT
