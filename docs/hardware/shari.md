# SHARI

Kits4Hams `SHARI` can be used two ways:

- recommended first path: Dire Wolf handles the modem and pyBulletin uses
  `kiss_tcp`
- native pyBulletin path: pyBulletin handles Bell 202 audio directly with
  `afsk`

This page is specifically for the SHARI embedded-radio family, not the Kits4Hams
`DINAH` and `PAUL` interface boards.

## Fit

- Recommended mode: `kiss_tcp` through Dire Wolf
- Native/lab mode: `afsk`
- Audio path: board-integrated sound/audio path
- PTT path: usually GPIO or board-specific control wiring

## Recommended Dire Wolf Setup

Use this guide first:

- [Raspberry Pi 3B+ + SHARI Pi3V + Dire Wolf](direwolf-shari-pi.md)

## Native AFSK Example

```toml
[kiss]
transport = "afsk"

[afsk]
input_device  = "default"
output_device = "default"
sample_rate   = 48000
mark_hz       = 1200
space_hz      = 2200
baud          = 1200
ptt_device    = "gpio:23"
```

## What To Verify

- the correct ALSA input/output device is selected
- PTT wiring matches the configured GPIO selector
- transmit deviation is not too high
- receive audio is not clipped or heavily filtered

## Notes

The SA818/SA818S radio chain is voice-oriented, so packet performance depends
heavily on audio level and filtering. Expect real-world tuning work. Dire Wolf
is the safer first modem choice until pyBulletin's native `afsk` path has more
field time on this hardware.

For Kits4Hams `DINAH` and `PAUL`, see:
- [CM108 / CM119 Interfaces](/home/jdlewis/GitHub/pyBulletin/docs/hardware/cm108-interfaces.md)

For Kits4Hams `BRIAN`, see:
- [BRIAN](/home/jdlewis/GitHub/pyBulletin/docs/hardware/brian.md)
