# Hardware Guide

This guide groups packet-radio and AllStar-style audio/modem hardware by how
`pyBulletin` should talk to it.

The key distinction is:

- `afsk`: pyBulletin does Bell 202 modem work itself over a soundcard/audio path
- `kiss_serial` / `kiss_tcp`: an external modem or TNC already does the packet modem work

## Hardware Matrix

| Family | Examples | pyBulletin mode | Audio path | PTT / control path | Notes |
|---|---|---|---|---|---|
| CM108/CM119 USB radio interfaces | modified CM108/CM119 USB fobs, DMK `URI`, `URIx`, Repeater Builder `RIM-Lite`, Masters `RA-25/33/35/40/42`, `RA-DR1X`, Kits4Hams `DINAH`, Kits4Hams `PAUL` | `afsk` | USB soundcard | `cm108:/dev/hidrawN:<pin>` | Best fit for native AFSK when the board exposes C-Media HID GPIO |
| Generic USB soundcard interfaces | `SignaLink USB`, generic C-Media or similar USB audio dongles | `afsk` | USB soundcard | VOX, none, `serial_rts:...`, or external GPIO | Good fit when the interface is just audio and PTT is handled elsewhere |
| Pi codec / radio HATs | NW Digital Radio `UDRC`, `DRAWS` | `afsk` | ALSA / I2S codec | `gpio:<bcm_pin>` or `gpiochip:...` | These are soundcard-style interfaces, not CM108 HID devices |
| Integrated Pi radio boards | Kits4Hams `SHARI`, Kits4Hams `BRIAN` | `afsk` | board audio path | usually GPIO or board-specific control | Requires radio/audio-level tuning in addition to software setup |
| Legacy / specialty host interfaces | Quad Radio PCI and similar host-attached radio cards | likely `afsk` | host audio / board-specific | board-specific | Mentioned for scope; not specifically validated in-tree yet |
| USB / serial KISS TNCs | `TNC-X`, Kantronics KISS-capable units | `kiss_serial` | external modem | serial | Use when the hardware is already a modem/TNC |
| Network or software KISS endpoints | Dire Wolf, `soundmodem`, network KISS servers | `kiss_tcp` | external modem | TCP | Not a native-AFSK case from pyBulletin’s perspective |
| Bluetooth / appliance TNCs | `Mobilinkd TNC4` and similar | usually `kiss_serial` or future Bluetooth path | external modem | KISS/Bluetooth/serial | Treat as an external TNC, not a soundcard modem |

## AFSK Device Classes

### CM108 / CM119 Interfaces

These are the most important USB radio-interface class to document well because
they often expose both:

- a USB audio device for RX/TX audio
- a `hidraw` device for GPIO-based PTT/COS control

Use:

```toml
[kiss]
transport = "afsk"

[afsk]
input_device  = "hw:1,0"
output_device = "hw:1,0"
ptt_device    = "cm108:/dev/hidraw0:3"
```

`doctor-afsk` and `deploy/doctor.sh` both try to surface likely C-Media
`hidraw` devices.

Detailed recipe:
- [CM108 / CM119 Interfaces](/home/jdlewis/GitHub/pyBulletin/docs/hardware/cm108-interfaces.md)

### Generic USB Soundcards

These are plain audio interfaces with no special modem hardware.

Use:

```toml
[kiss]
transport = "afsk"

[afsk]
input_device  = "hw:1,0"
output_device = "hw:1,0"
ptt_device    = ""
```

If the interface uses VOX, leaving `ptt_device` empty is often correct.
If it has a separate serial-keying path, use `serial_rts:/dev/ttyUSB0`.

Detailed recipe:
- [SignaLink USB](/home/jdlewis/GitHub/pyBulletin/docs/hardware/signalink.md)

### Pi Codec Boards

Boards like `UDRC` and `DRAWS` behave more like onboard soundcards than USB
HID-radio interfaces.

Use:

```toml
[kiss]
transport = "afsk"

[afsk]
input_device  = "default"
output_device = "default"
ptt_device    = "gpio:23"
```

The important point is that these usually want GPIO or `gpiochip` PTT, not the
CM108 `hidraw` path.

Detailed recipe:
- [UDRC / DRAWS](/home/jdlewis/GitHub/pyBulletin/docs/hardware/udrc-draws.md)

### Integrated Radio Boards

Boards like `SHARI` combine the radio and interface logic more tightly.

They still belong under `afsk`, but they need extra care for:

- RX/TX audio levels
- deviation
- filtering / pre-emphasis behavior in the radio chain
- whatever PTT/control path the board exposes

Detailed recipe:
- [SHARI](/home/jdlewis/GitHub/pyBulletin/docs/hardware/shari.md)
- [BRIAN](/home/jdlewis/GitHub/pyBulletin/docs/hardware/brian.md)

## KISS Device Classes

### Hardware KISS TNCs

Use when the hardware already presents a packet modem/TNC interface.

```toml
[kiss]
transport = "kiss_serial"
device    = "/dev/ttyUSB0"
baud      = 9600
```

### TCP KISS Endpoints

Use when some other modem process or network endpoint already speaks KISS.

```toml
[kiss]
transport = "kiss_tcp"
tcp_host  = "127.0.0.1"
tcp_port  = 8001
```

## Other Scoped Hardware

### Quad Radio PCI And Similar Host Cards

These are worth naming in the hardware matrix because they show up in AllStar
and radio-interface discussions, but they are not yet specifically validated in
this tree.

For now, treat them as:

- potentially `afsk` if they expose usable Linux audio/control paths
- board-specific until real validation confirms the right control model

## PTT Selector Matrix

| Selector | Intended hardware | Example |
|---|---|---|
| empty string | VOX, manual keying, or no PTT control in software | `ptt_device = ""` |
| `serial_rts:/dev/ttyUSB0` | serial adapters, USB CAT/PTT dongles, some soundcard interfaces | `ptt_device = "serial_rts:/dev/ttyUSB0"` |
| `gpio:23` | Raspberry Pi BCM GPIO | `ptt_device = "gpio:23"` |
| `gpiochip:/dev/gpiochip0:24` | libgpiod-based GPIO line access | `ptt_device = "gpiochip:/dev/gpiochip0:24"` |
| `cm108:/dev/hidraw0:3` | CM108/CM119 family USB interfaces | `ptt_device = "cm108:/dev/hidraw0:3"` |

## Setup Flow

### Native AFSK

1. Set `[kiss].transport = "afsk"`.
2. Install the audio dependency:
   `python -m pip install -e ".[audio]"`
3. Choose the input/output audio device.
4. Choose the correct `ptt_device` selector or leave it empty for VOX.
5. Run:
   `pybulletin --config config/pybulletin.local.toml doctor-afsk`
6. On deployed systems, also run:
   `sudo bash deploy/doctor.sh`

### External TNC / Modem

1. Set `[kiss].transport = "kiss_serial"` or `"kiss_tcp"`.
2. Configure the serial or TCP endpoint.
3. Leave `[afsk]` present but unused.

## Current Limits

Native AFSK support in-tree now includes RX/TX Bell 202 audio and multiple PTT
control paths, but the following still need field hardening:

- DCD / COS integration
- stronger symbol timing and carrier recovery under noisy conditions
- per-device tuning guidance for real-world radio audio chains
- more hardware validation across specific boards and interface revisions
