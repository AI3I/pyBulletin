# Helper Scripts

This file is the operator reference for scripts that live outside the Python
package entry point. All paths are relative to the repository root unless noted.

## Deployment Helpers

Run these as root on the target host. They share defaults from `deploy/lib.sh`
and install into `/home/pybulletin/pyBulletin` unless overridden with
environment variables.

| Script | Purpose |
|--------|---------|
| `deploy/install.sh` | First-time install. Creates the `pybulletin` user, installs runtime packages, syncs the tree, installs config if missing, installs systemd/fail2ban/logrotate/udev files, bootstraps `SYSOP`, starts `pybulletin.service` and `pybulletinweb.service`, and enables timers. |
| `deploy/upgrade.sh` | Upgrade an existing install. Pulls the source repo when possible, backs up existing config, syncs the tree, refreshes units and fail2ban/logrotate files, and restarts both core and web services. |
| `deploy/repair.sh` | Reapply deployment state after drift or partial failure. Reinstalls units, packages, fail2ban/logrotate files, optional config files, and restarts both services. |
| `deploy/uninstall.sh` | Removes installed units, fail2ban snippets, logrotate config, and bootstrap note. Keeps `data/` and `config/` by default; set `KEEP_DATA=0 KEEP_CONFIG=0` for a full app directory removal. |
| `deploy/doctor.sh` | Prints service, config, database, fail2ban, SELinux, AX.25, AFSK audio, PTT, CM108/CM119, and API health status. |
| `deploy/migrate.sh` | Imports LinFBB/FBB data through `scripts/migrate_fbb.py`. Requires `--from-fbb PATH`; accepts `--config PATH` and `--dry-run`. |
| `deploy/strings.sh` | Pushes `config/strings.toml` to a remote node over `rsync`; the running node hot-reloads strings within about 30 seconds. |
| `deploy/setup-nginx.sh` | Installs nginx and proxies the single `pybulletinweb.service` backend. Public UI is `/`; sysop console is `/sysop`. Use `--domain DOMAIN`, optional `--email EMAIL`, `--no-tls`, and `--web-port PORT`. |

Common examples:

```bash
sudo bash deploy/install.sh
git pull --ff-only
sudo bash deploy/upgrade.sh
sudo bash deploy/repair.sh
sudo bash deploy/doctor.sh
sudo bash deploy/migrate.sh --from-fbb /fbb --dry-run
bash deploy/strings.sh root@pybulletin.ai3i.net
sudo bash deploy/setup-nginx.sh --domain bbs.example.net --email admin@example.net
KEEP_DATA=0 KEEP_CONFIG=0 sudo -E bash deploy/uninstall.sh
```

## Installed Services

| Unit | Purpose |
|------|---------|
| `pybulletin.service` | Core BBS service: Telnet, RF transports, inbound B2F forwarding, and forwarding scheduler. |
| `pybulletinweb.service` | Web service: public BBS UI, sysop console, JSON API, and WebSocket endpoint. |
| `pybulletin-forward.service` / `.timer` | One-shot scheduled forwarding helper. |
| `pybulletin-retention.service` / `.timer` | One-shot message retention cleanup helper. |

The old `pybulletin-web.service` name is obsolete. Install, upgrade, and repair
remove it and reset any failed state during migration.

## Upgrade Path

For an existing deployment:

```bash
cd /path/to/pyBulletin
git pull --ff-only
sudo bash deploy/upgrade.sh
sudo bash deploy/doctor.sh
```

`upgrade.sh` creates a timestamped config backup before syncing files, refreshes
systemd/fail2ban/logrotate assets, restarts `pybulletin.service` and
`pybulletinweb.service`, and waits for both to become active. Existing data and
config stay in `/home/pybulletin/pyBulletin` unless you intentionally remove
them.

## Navigating `pybulletinweb.service`

`pybulletinweb.service` is the standalone web/API process. It does not replace
`pybulletin.service`; both should be active on a normal node.

Useful commands:

```bash
systemctl status pybulletinweb.service
journalctl -u pybulletinweb.service -n 100 --no-pager
systemctl restart pybulletinweb.service
curl -fsS http://127.0.0.1:8080/api/health
```

Default web paths:

| Path | Purpose |
|------|---------|
| `/` | Public BBS UI, shown when `[public_web] enabled = true`. |
| `/sysop` | Sysop console login page. |
| `/api/health` | Unauthenticated health check. |

The service defaults to `127.0.0.1:8080`. For remote access, proxy it through
nginx with:

```bash
sudo bash deploy/setup-nginx.sh --domain bbs.example.net --email admin@example.net
```

## Deployment Environment Overrides

These variables are read by `deploy/lib.sh` and therefore apply to install,
upgrade, repair, uninstall, doctor, and migrate helpers.

| Variable | Default |
|----------|---------|
| `PYBULLETIN_USER` | `pybulletin` |
| `PYBULLETIN_GROUP` | same as `PYBULLETIN_USER` |
| `PYBULLETIN_HOME` | `/home/$PYBULLETIN_USER` |
| `PYBULLETIN_APP_DIR` | `$PYBULLETIN_HOME/pyBulletin` |
| `PYBULLETIN_SERVICE_NAME` | `pybulletin.service` |
| `PYBULLETIN_WEB_SERVICE_NAME` | `pybulletinweb.service` |
| `PYBULLETIN_CONFIG_DEST` | `$PYBULLETIN_APP_DIR/config/pybulletin.toml` |
| `PYBULLETIN_PKG_AUTO_INSTALL` | `1` |
| `PYBULLETIN_PYTHON_LINK` | `/usr/local/bin/pybulletin-python` |
| `PYBULLETIN_SYSOP_BOOTSTRAP_NOTE` | `/root/pybulletin-initial-sysop.txt` |
| `PYBULLETIN_CONFIG_BACKUP_DIR` | `$PYBULLETIN_APP_DIR/config/backups` |

## Python Maintenance Helpers

These can be run directly from a checkout or from the installed tree. Set
`PYTHONPATH=src` when running from source without an editable install.

| Script | Purpose |
|--------|---------|
| `scripts/bootstrap_sysop.py` | Creates the initial `SYSOP` account and writes the bootstrap note. Called by install and repair. |
| `scripts/backup.py` | Online SQLite backup using the sqlite3 backup API. Safe while services are running. |
| `scripts/cleanup_retention.py` | Runs message retention cleanup once. The retention systemd timer calls this daily. |
| `scripts/migrate_fbb.py` | Imports basic LinFBB/FBB messages and users into the configured SQLite store. |

Examples:

```bash
PYTHONPATH=src python scripts/backup.py --config config/pybulletin.toml
PYTHONPATH=src python scripts/backup.py --config config/pybulletin.toml --dest /var/backups/pybulletin
PYTHONPATH=src python scripts/cleanup_retention.py --config config/pybulletin.toml
PYTHONPATH=src python scripts/migrate_fbb.py --config config/pybulletin.toml --source /fbb --dry-run
```

## CLI Diagnostics

The `pybulletin` console command also has operational helpers:

| Command | Purpose |
|---------|---------|
| `pybulletin --config PATH doctor` | Prints local config and listener summary. |
| `pybulletin --config PATH doctor-rf` | Checks userspace RF readiness, including KISS serial/TCP and AFSK configuration. |
| `pybulletin --config PATH doctor-afsk` | Checks native Bell 202 soundcard/PTT support and device selectors. |
| `pybulletin --config PATH validate-config` | Validates configuration and exits non-zero on errors. |
| `pybulletin --config PATH test-ptt` | Keys the configured AFSK PTT briefly and releases it. |
| `pybulletin --config PATH run-forward` | Runs one outbound forwarding cycle. |
| `pybulletin --config PATH run-retention` | Runs one message retention cleanup cycle. |

Use `test-ptt --selector SELECTOR --duration SECONDS` to test a selector
without editing the main config.
