# CM108 / CM119 Interfaces

This class includes many USB radio interfaces based on C-Media CM108/CM119
audio chips with HID GPIO available for PTT.

Typical examples:

- modified generic `CM108` / `CM119` USB audio fobs
- DMK `URI`, `URIx`
- Repeater Builder `RIM-Lite`
- Masters Communications `RA-25`, `RA-33`, `RA-35`, `RA-40`, `RA-42`, `RA-DR1X`
- Kits4Hams `DINAH`
- Kits4Hams `PAUL`

## Fit

- Mode: `afsk`
- Audio path: USB soundcard
- PTT path: `cm108:/dev/hidrawN:<pin>`

## Example

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
ptt_device    = "cm108:/dev/hidraw0:3"
```

## What To Verify

- the matching USB audio device is selected
- the `hidraw` device belongs to the same interface family
- the chosen CM108 GPIO pin is the board’s actual PTT pin
- the service user has `audio` group access

## Kits4Hams Variants

### DINAH

`DINAH` is a Kits4Hams USB radio interface intended for radios with a packet /
data miniDIN connector. Kits4Hams describes it as using a `CM119B` or `CM108B`
USB audio codec and notes support for Dire Wolf-style use, including 1200/9600
selection jumpers.

For `pyBulletin`, treat it as:

- mode: `afsk`
- audio path: USB soundcard
- control path: likely `cm108:/dev/hidrawN:<pin>` when HID GPIO is used

The main board-specific consideration is that DINAH is designed around the
radio's packet/data connector rather than a fully custom cable harness.

### PAUL

`PAUL` is a Kits4Hams USB radio interface with a DE9 connector and optically
coupled solid-state relays for COS and PTT. Kits4Hams also describes it as
using a `CM119B` or `CM108B` USB audio codec.

For `pyBulletin`, treat it as:

- mode: `afsk`
- audio path: USB soundcard
- control path: usually still documented under the CM108/119 USB-interface
  family, but actual board wiring may expose relay-driven PTT/COS behavior
  rather than a bare radio-data connector

The practical implication is that PAUL belongs in the same software bucket as
URI/RIM/Masters-CM108 devices, but field validation should confirm which CM108
GPIO pin is actually wired to PTT on the assembled board.

### Generic Modified CM108 / CM119 USB Fobs

This is the broad DIY class: a plain USB C-Media audio dongle modified or wired
for radio audio and PTT.

For `pyBulletin`, treat it as:

- mode: `afsk`
- audio path: USB soundcard
- control path: `cm108:/dev/hidrawN:<pin>` if the HID GPIO line is exposed

These belong in the same software bucket as URI/RIM/Masters/DINAH/PAUL devices
even if they are not sold as a complete radio interface product.

## Helpful Commands

```bash
pybulletin --config config/pybulletin.local.toml doctor-afsk
sudo bash deploy/doctor.sh
ls -l /dev/hidraw*
```

## Notes

The deployed udev rule gives `audio` group access to C-Media `hidraw` devices.
That is the intended path for this hardware family.

That family includes both branded radio interfaces and DIY / modified C-Media
USB fobs, as long as the HID GPIO path is actually wired and exposed.
