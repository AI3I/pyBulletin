# UDRC / DRAWS

NW Digital Radio `UDRC` and `DRAWS` boards should be treated as native `afsk`
interfaces, not as CM108 HID devices.

## Fit

- Mode: `afsk`
- Audio path: ALSA / I2S codec on the Pi
- PTT path: `gpio:<bcm_pin>` or `gpiochip:/dev/gpiochipX:<line>`

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
ptt_device    = "gpio:23"
```

## What To Verify

- the ALSA device appears and is stable across boots
- the board’s PTT line matches the configured GPIO
- Pi audio stack and overlays are configured correctly
- the board-level transmit and receive gain settings are sane

## Notes

These boards often work well for packet, but they need Pi-specific audio and
GPIO setup rather than CM108 `hidraw` handling.
