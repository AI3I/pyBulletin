# BRIAN

Kits4Hams `BRIAN` should be treated as an integrated-radio native `afsk`
interface, similar in deployment shape to `SHARI`, but not based on the SA818
module family.

## Fit

- Mode: `afsk`
- Audio path: board/radio-integrated USB interface
- PTT path: board-specific control path

## Example

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
ptt_device    = ""
```

## What To Verify

- the board appears as the expected USB audio device
- the integrated radio path does not overdrive TX audio
- receive audio is not clipped or excessively filtered
- any board-specific PTT/COS path is understood before enabling software PTT

## Notes

`BRIAN` belongs with integrated radio/interface boards, not the CM108 HID
interface family. It should be tuned like a self-contained radio node rather
than a generic external soundcard interface.
