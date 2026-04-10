# SignaLink USB

`SignaLink USB` is best treated as a generic USB soundcard interface under
native `afsk`.

## Fit

- Mode: `afsk`
- Audio path: USB soundcard
- PTT path: usually VOX, sometimes none in software

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
ptt_device    = ""
```

## What To Verify

- the correct USB audio device is selected
- VOX reliably keys TX without clipping the start of packets
- TX audio level is not overdriving the radio
- RX audio is clean and not over-amplified

## Notes

For SignaLink-style interfaces, leaving `ptt_device` empty is often correct.
If the station uses a separate serial PTT path, `serial_rts:/dev/ttyUSB0` is
the cleaner software-keying option.
