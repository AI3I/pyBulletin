# Raspberry Pi 3B+ + SHARI Pi3V + Dire Wolf

This is the recommended first RF deployment path for a Raspberry Pi 3B+ with a
Kits4Hams SHARI Pi3V / SA818 board. In this mode Dire Wolf handles the Bell 202
modem and radio interface, while pyBulletin talks to Dire Wolf as a TCP KISS
TNC.

Use this before trying pyBulletin's native `afsk` transport. It gives better
known modem behavior and keeps pyBulletin focused on BBS, AX.25 session, and
forwarding logic.

## Topology

```text
RF <-> SHARI / SA818 <-> ALSA audio + PTT <-> Dire Wolf <-> TCP KISS 8001 <-> pyBulletin
```

pyBulletin does not need Linux kernel AX.25, `kissattach`, or `mkiss` for this
setup. It connects directly to Dire Wolf's TCP KISS listener.

## Install Packages

On Raspberry Pi OS / Debian-family systems:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip direwolf alsa-utils
```

Install pyBulletin from a checkout:

```bash
git clone https://github.com/AI3I/pyBulletin.git
cd pyBulletin
sudo bash deploy/install.sh
```

The deploy script installs pyBulletin into `/home/pybulletin/pyBulletin`,
creates the `pybulletin` user, enables `pybulletin.service` and
`pybulletinweb.service`, and writes the initial SYSOP credential note to:

```bash
/root/pybulletin-initial-sysop.txt
```

## Identify Audio Devices

List the ALSA capture/playback devices:

```bash
arecord -l
aplay -l
```

On a Pi with a SHARI-style board, the right device may be the default ALSA
device or a specific `hw:CARD,DEVICE` pair. Keep the device name stable once it
is working.

## Configure Dire Wolf

Create or edit the Dire Wolf config. The exact `ADEVICE` value depends on the
audio device discovered above.

Example `/home/pybulletin/direwolf.conf`:

```text
ADEVICE default
CHANNEL 0
MYCALL N0BBS-1
MODEM 1200
KISSPORT 8001
AGWPORT 0
```

Notes:

- `KISSPORT 8001` is the TCP KISS listener pyBulletin will use.
- `AGWPORT 0` disables the AGW listener when it is not needed.
- Replace `N0BBS-1` with the BBS node callsign.
- SHARI/SA818 performance depends heavily on audio level and deviation.

Run Dire Wolf manually first:

```bash
sudo -u pybulletin direwolf -c /home/pybulletin/direwolf.conf -t 0
```

In another shell, confirm the KISS port is listening:

```bash
ss -ltnp | grep ':8001'
```

## Run Dire Wolf As A Service

Create `/etc/systemd/system/direwolf.service`:

```ini
[Unit]
Description=Dire Wolf software TNC for pyBulletin
After=sound.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pybulletin
Group=pybulletin
WorkingDirectory=/home/pybulletin
ExecStart=/usr/bin/direwolf -c /home/pybulletin/direwolf.conf -t 0
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now direwolf.service
sudo systemctl status direwolf.service
```

## Configure pyBulletin For Dire Wolf

Edit `/home/pybulletin/pyBulletin/config/pybulletin.toml`:

```toml
[node]
node_call = "N0BBS-1"

[kiss]
transport = "kiss_tcp"
tcp_host  = "127.0.0.1"
tcp_port  = 8001
paclen    = 236
```

Make sure `direwolf.service` starts before the pyBulletin core service. Add a
systemd drop-in:

```bash
sudo systemctl edit pybulletin.service
```

Use:

```ini
[Unit]
After=direwolf.service
Wants=direwolf.service
```

Then restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart direwolf.service pybulletin.service pybulletinweb.service
```

## Validate

Run pyBulletin's deployment and RF checks:

```bash
sudo bash /home/pybulletin/pyBulletin/deploy/doctor.sh
sudo -u pybulletin /usr/local/bin/pybulletin-python \
  -m pybulletin.cli \
  --config /home/pybulletin/pyBulletin/config/pybulletin.toml \
  doctor-rf
```

Expected high-level results:

- `pybulletin.service` is active.
- `pybulletinweb.service` is active.
- `direwolf.service` is active.
- `kiss_tcp` points to `127.0.0.1:8001`.
- Kernel AX.25 may be unavailable; that is acceptable for this setup.

Check listeners:

```bash
ss -ltnp | grep -E ':(6300|6301|8001|8080)\b'
```

Expected defaults:

| Port | Owner | Purpose |
|------|-------|---------|
| `6300` | `pybulletin.service` | BBS Telnet |
| `6301` | `pybulletin.service` | B2F forward listener |
| `8001` | `direwolf.service` | TCP KISS |
| `8080` | `pybulletinweb.service` | Web/sysop/API, loopback by default |

## Sysop Web Console

The sysop console is served by `pybulletinweb.service` at:

```text
http://127.0.0.1:8080/sysop
```

On a headless Pi, proxy it with nginx:

```bash
cd /home/pybulletin/pyBulletin
sudo bash deploy/setup-nginx.sh --domain bbs.example.net --email admin@example.net
```

Log in with callsign `SYSOP` and the generated password from:

```bash
sudo cat /root/pybulletin-initial-sysop.txt
```

## Common Failures

### `doctor-rf` cannot connect to `127.0.0.1:8001`

Dire Wolf is not running, did not load the config you edited, or does not have
`KISSPORT 8001` enabled.

Check:

```bash
systemctl status direwolf.service
journalctl -u direwolf.service -n 100 --no-pager
ss -ltnp | grep ':8001'
```

### Dire Wolf has no audio device

Check ALSA devices and service permissions:

```bash
arecord -l
aplay -l
id pybulletin
journalctl -u direwolf.service -n 100 --no-pager
```

The `pybulletin` user should be in the `audio` group after deploy.

### RF decodes but transmit does not work

This is usually PTT, TX audio level, or SA818 deviation/filtering. Validate with
Dire Wolf alone before changing pyBulletin. pyBulletin only sees the KISS side
of the link in this mode.

### Web works locally but not from another machine

`pybulletinweb.service` intentionally binds to loopback by default. Use nginx or
an SSH tunnel for remote access instead of changing `[web].host` to `0.0.0.0`
unless the network is otherwise protected.

## References

- Dire Wolf upstream: <https://github.com/wb2osz/direwolf>
- Dire Wolf additional documentation: <https://github.com/wb2osz/direwolf-doc>
