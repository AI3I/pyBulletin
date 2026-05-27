# External Radio Interface Families

This guide maps common radio-interface hardware to the most practical
pyBulletin transport choices. Most of these devices are not BBS-specific; they
expose audio, PTT, COS, or an external KISS modem path that pyBulletin can use.

## Recommended Rule

Use this order of preference:

1. `kiss_tcp` through Dire Wolf or soundmodem when you want the quickest working
   RF node.
2. `kiss_serial` when the device is already a hardware KISS TNC.
3. native `afsk` when you specifically want pyBulletin to perform Bell 202 audio
   modem work itself.

Native `afsk` is useful and improving, but it still needs more field time than
Dire Wolf on noisy real-world channels.

## Families

| Family | Examples | Recommended first mode | Native AFSK fit | PTT/control notes |
|---|---|---|---|---|
| C-Media USB GPIO interfaces | Kits4Hams DINAH / PAUL, DMK URI/URIx, Repeater Builder RIM, Masters RA-25/33/35/40/42, many modified CM108/CM119 fobs | `kiss_tcp` with Dire Wolf | yes | often `cm108:/dev/hidrawN:<pin>` |
| Masters Communications data/radio adapters | RA and DRA family boards | `kiss_tcp` with Dire Wolf | often yes | board-specific; many are C-Media USB audio/control devices |
| DigiRig Mobile and similar USB audio/CAT/PTT interfaces | DigiRig Mobile, generic USB audio plus serial/CAT/PTT | `kiss_tcp` with Dire Wolf | yes | often `serial_rts:/dev/ttyUSB0`, CAT outside pyBulletin, or VOX |
| VOX audio interfaces | SignaLink USB, generic USB audio into radio VOX | `kiss_tcp` with Dire Wolf | yes | usually `ptt_device = ""` |
| Pi codec/HAT interfaces | UDRC, DRAWS, other Pi audio HATs | `kiss_tcp` with Dire Wolf | yes | usually `gpio:` or `gpiochip:` |
| Integrated Pi radio boards | SHARI Pi3V / SA818, BRIAN-like integrated boards | `kiss_tcp` with Dire Wolf | lab/field validation | GPIO or board-specific PTT, careful audio/deviation tuning |
| Hardware TNCs | TNC-X, Kantronics KISS-capable TNCs, Mobilinkd in serial mode | `kiss_serial` | no | external TNC owns modem/PTT |

## DINAH

Kits4Hams DINAH belongs in the C-Media USB GPIO interface family. It is not a
SHARI-style integrated radio. Treat it like a USB soundcard/radio interface
connected to an external radio's packet/data connector.

Recommended first path:

```toml
[kiss]
transport = "kiss_tcp"
tcp_host = "127.0.0.1"
tcp_port = 8001
```

Native AFSK path:

```toml
[kiss]
transport = "afsk"

[afsk]
input_device  = "hw:1,0"
output_device = "hw:1,0"
ptt_device    = "cm108:/dev/hidraw0:3"
```

The exact HID GPIO pin must be confirmed against the board revision and wiring.

## Masters Communications RA / DRA

Masters Communications RA and DRA interfaces should be treated by the Linux
interface they expose:

- USB audio plus C-Media HID GPIO: CM108/CM119 family
- USB audio plus separate PTT serial/control path: generic USB soundcard family
- board-specific GPIO on a Pi: Pi codec/HAT family

Recommended first path is still Dire Wolf or soundmodem with pyBulletin using
`kiss_tcp`. Move to native `afsk` after audio/PTT are confirmed.

## DigiRig Mobile

DigiRig-style interfaces commonly expose USB audio plus serial/CAT/PTT
functions. pyBulletin does not need to own CAT control when Dire Wolf is acting
as the modem.

Recommended first path:

- configure Dire Wolf for the DigiRig audio device and PTT method
- configure pyBulletin as `kiss_tcp`

Possible native AFSK path:

```toml
[kiss]
transport = "afsk"

[afsk]
input_device  = "hw:1,0"
output_device = "hw:1,0"
ptt_device    = "serial_rts:/dev/ttyUSB0"
```

If PTT is done by VOX or by another application, leave `ptt_device = ""`.

## Diagnostics

Useful checks:

```bash
arecord -l
aplay -l
ls -l /dev/hidraw* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
pybulletin --config config/pybulletin.local.toml doctor-rf
pybulletin --config config/pybulletin.local.toml doctor-afsk
sudo bash deploy/doctor.sh
```

For C-Media HID GPIO PTT, `deploy/doctor.sh` and `doctor-afsk` try to show
likely `hidraw` devices.
