"""Direct Bell 202 AFSK transport over soundcard audio.

This is the future home of the in-process modem path for USB soundcards and
radio interfaces such as SHARI-class devices.  It sits below AX.25 and above
the eventual audio/PTT/DCD implementation.

Current status:
  - configuration plumbing is present
  - the transport surface is wired into the CLI
  - HDLC helpers exist in ``pybulletin.ax25.hdlc``
  - actual audio DSP and PTT control are not implemented yet
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from array import array
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from ..ax25.hdlc import encode_hdlc_frame, extract_hdlc_frames, nrzi_encode

if TYPE_CHECKING:
    from ..ax25.router import AX25Router
    from ..ax25.frame import AX25Frame
    from ..config import AfskConfig

LOG = logging.getLogger(__name__)
_DEFAULT_TX_LEVEL = 0.6
_DEFAULT_PREAMBLE_FLAGS = 32
_DEFAULT_POSTAMBLE_FLAGS = 4


class Bell202Demodulator:
    """Bell 202 AFSK demodulator for 1200-baud AX.25 receive.

    This implementation is intentionally simple and pure Python:
      - one symbol decision per nominal symbol period
      - Goertzel power estimate at mark/space
      - NRZI decode
      - HDLC flag/FCS validation

    It is good enough for synthetic tests and a first live soundcard path,
    but it is not yet a hardened modem with symbol-clock recovery, filtering,
    AGC, or squelch/COS integration.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        baud: int,
        mark_hz: int,
        space_hz: int,
    ) -> None:
        self._sample_rate = sample_rate
        self._baud = baud
        self._mark_hz = mark_hz
        self._space_hz = space_hz
        self._samples_per_symbol = sample_rate / float(baud)
        self._sym_frac = 0.0
        self._sample_buf: list[float] = []
        self._last_level = 1
        self._raw_bits: list[int] = []

    def feed_samples(self, samples: Iterable[float]) -> list[bytes]:
        self._sample_buf.extend(float(s) for s in samples)
        levels: list[int] = []
        while True:
            n = self._next_symbol_len()
            if len(self._sample_buf) < n:
                break
            window = self._sample_buf[:n]
            del self._sample_buf[:n]
            levels.append(self._classify_symbol(window))

        if not levels:
            return []

        for level in levels:
            bit = 1 if level == self._last_level else 0
            self._raw_bits.append(bit)
            self._last_level = level

        payloads, remainder = extract_hdlc_frames(self._raw_bits)
        self._raw_bits = remainder
        return payloads

    def _next_symbol_len(self) -> int:
        self._sym_frac += self._samples_per_symbol
        n = int(self._sym_frac)
        if n <= 0:
            n = 1
        self._sym_frac -= n
        return n

    def _classify_symbol(self, window: list[float]) -> int:
        mark = self._goertzel_power(window, self._mark_hz)
        space = self._goertzel_power(window, self._space_hz)
        return 1 if mark >= space else 0

    def _goertzel_power(self, window: list[float], freq_hz: int) -> float:
        if not window:
            return 0.0
        omega = 2.0 * math.pi * (freq_hz / self._sample_rate)
        coeff = 2.0 * math.cos(omega)
        s_prev = 0.0
        s_prev2 = 0.0
        for sample in window:
            s = sample + coeff * s_prev - s_prev2
            s_prev2 = s_prev
            s_prev = s
        return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


class Bell202Modulator:
    """Bell 202 AFSK modulator for AX.25 transmit."""

    def __init__(
        self,
        *,
        sample_rate: int,
        baud: int,
        mark_hz: int,
        space_hz: int,
        level: float = _DEFAULT_TX_LEVEL,
        preamble_flags: int = _DEFAULT_PREAMBLE_FLAGS,
        postamble_flags: int = _DEFAULT_POSTAMBLE_FLAGS,
    ) -> None:
        self._sample_rate = sample_rate
        self._baud = baud
        self._mark_hz = mark_hz
        self._space_hz = space_hz
        self._level = max(0.0, min(0.95, level))
        self._preamble_flags = max(1, preamble_flags)
        self._postamble_flags = max(1, postamble_flags)

    def modulate_ax25_frame(self, payload: bytes) -> bytes:
        """Return mono PCM16LE Bell 202 audio for one AX.25 payload."""
        framed = encode_hdlc_frame(payload)
        preamble = bytes([0x7E]) * self._preamble_flags
        postamble = bytes([0x7E]) * self._postamble_flags
        bits = []
        for data in (preamble, framed, postamble):
            for byte in data:
                for bit in range(8):
                    bits.append((byte >> bit) & 0x01)
        levels = nrzi_encode(bits, initial=1)
        return _levels_to_pcm16le(
            levels,
            sample_rate=self._sample_rate,
            baud=self._baud,
            mark_hz=self._mark_hz,
            space_hz=self._space_hz,
            level=self._level,
        )


class _PTTControl:
    async def set_keyed(self, keyed: bool) -> None:
        return None

    async def close(self) -> None:
        return None


class _NullPTT(_PTTControl):
    pass


class _SerialRTSPTT(_PTTControl):
    def __init__(self, device: str) -> None:
        self._device = device
        self._serial = None

    def _ensure_open(self) -> None:
        if self._serial is not None:
            return
        try:
            import serial  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "serial RTS PTT requires pyserial; install pybulletin[tnc]"
            ) from exc
        self._serial = serial.Serial(self._device)
        self._serial.rts = False

    async def set_keyed(self, keyed: bool) -> None:
        await asyncio.to_thread(self._set_keyed_sync, keyed)

    def _set_keyed_sync(self, keyed: bool) -> None:
        self._ensure_open()
        assert self._serial is not None
        self._serial.rts = bool(keyed)

    async def close(self) -> None:
        if self._serial is not None:
            serial_port = self._serial
            self._serial = None
            await asyncio.to_thread(serial_port.close)


class _RPiGpioPTT(_PTTControl):
    def __init__(self, pin: int, *, active_high: bool = True) -> None:
        self._pin = pin
        self._active_high = active_high
        self._gpio = None

    def _ensure_open(self) -> None:
        if self._gpio is not None:
            return
        try:
            import RPi.GPIO as gpio  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "BCM GPIO PTT requires RPi.GPIO"
            ) from exc
        gpio.setwarnings(False)
        gpio.setmode(gpio.BCM)
        initial = gpio.HIGH if not self._active_high else gpio.LOW
        gpio.setup(self._pin, gpio.OUT, initial=initial)
        self._gpio = gpio

    async def set_keyed(self, keyed: bool) -> None:
        await asyncio.to_thread(self._set_keyed_sync, keyed)

    def _set_keyed_sync(self, keyed: bool) -> None:
        self._ensure_open()
        assert self._gpio is not None
        value = self._gpio.HIGH if (keyed == self._active_high) else self._gpio.LOW
        self._gpio.output(self._pin, value)

    async def close(self) -> None:
        if self._gpio is not None:
            gpio = self._gpio
            self._gpio = None
            await asyncio.to_thread(gpio.cleanup, self._pin)


class _GpiodPTT(_PTTControl):
    def __init__(self, chip: str, line: int, *, active_high: bool = True) -> None:
        self._chip_name = chip
        self._line = line
        self._active_high = active_high
        self._chip = None
        self._line_handle = None
        self._request = None

    def _ensure_open(self) -> None:
        if self._request is not None or self._line_handle is not None:
            return
        try:
            import gpiod  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "gpiochip PTT requires python gpiod bindings"
            ) from exc

        # libgpiod v2 API
        if hasattr(gpiod, "request_lines"):
            direction = gpiod.line.Direction.OUTPUT
            value = gpiod.line.Value.INACTIVE if self._active_high else gpiod.line.Value.ACTIVE
            settings = gpiod.LineSettings(direction=direction, output_value=value)
            self._request = gpiod.request_lines(
                self._chip_name,
                consumer="pybulletin-afsk",
                config={self._line: settings},
            )
            return

        # libgpiod v1 API
        chip = gpiod.Chip(self._chip_name)
        line = chip.get_line(self._line)
        default_val = 0 if self._active_high else 1
        line.request(consumer="pybulletin-afsk", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[default_val])
        self._chip = chip
        self._line_handle = line

    async def set_keyed(self, keyed: bool) -> None:
        await asyncio.to_thread(self._set_keyed_sync, keyed)

    def _set_keyed_sync(self, keyed: bool) -> None:
        self._ensure_open()
        value = 1 if (keyed == self._active_high) else 0
        if self._request is not None:
            import gpiod  # type: ignore[import]
            request_value = gpiod.line.Value.ACTIVE if value else gpiod.line.Value.INACTIVE
            self._request.set_value(self._line, request_value)
            return
        assert self._line_handle is not None
        self._line_handle.set_value(value)

    async def close(self) -> None:
        if self._request is not None:
            request = self._request
            self._request = None
            await asyncio.to_thread(request.release)
        if self._line_handle is not None:
            line = self._line_handle
            self._line_handle = None
            await asyncio.to_thread(line.release)
        if self._chip is not None:
            chip = self._chip
            self._chip = None
            close_fn = getattr(chip, "close", None)
            if close_fn is not None:
                await asyncio.to_thread(close_fn)


class _CM108PTT(_PTTControl):
    def __init__(self, hidraw_device: str, pin: int, *, active_high: bool = True) -> None:
        self._hidraw_device = hidraw_device
        self._pin = pin
        self._active_high = active_high

    async def set_keyed(self, keyed: bool) -> None:
        await asyncio.to_thread(self._set_keyed_sync, keyed)

    def _set_keyed_sync(self, keyed: bool) -> None:
        if self._pin < 1 or self._pin > 8:
            raise RuntimeError(f"CM108 GPIO pin must be 1..8, got {self._pin}")
        state = 1 if (keyed == self._active_high) else 0
        mask = 1 << (self._pin - 1)
        data = state << (self._pin - 1)
        report = bytes((0, 0, mask, data, 0))
        fd = os.open(self._hidraw_device, os.O_WRONLY)
        try:
            written = os.write(fd, report)
            if written != len(report):
                raise RuntimeError(
                    f"short write to {self._hidraw_device}: {written}/{len(report)} bytes"
                )
        finally:
            os.close(fd)


class AfskBell202Link:
    """Bell 202 AFSK transport for direct soundcard operation."""

    def __init__(self, cfg: AfskConfig, router: AX25Router) -> None:
        self._cfg = cfg
        self._router = router
        self._task: asyncio.Task | None = None
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="afsk-bell202")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send_frame(self, frame: AX25Frame, port: int = 0) -> None:
        """Queue one AX.25 frame for transmission."""
        await self._tx_queue.put(frame.encode())

    async def _run(self) -> None:
        try:
            import sounddevice  # type: ignore[import]
        except ImportError:
            LOG.error(
                "afsk: sounddevice not installed. RX modem path is available "
                "in-code, but live soundcard capture requires: pip install sounddevice"
            )
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)
        demod = Bell202Demodulator(
            sample_rate=self._cfg.sample_rate,
            baud=self._cfg.baud,
            mark_hz=self._cfg.mark_hz,
            space_hz=self._cfg.space_hz,
        )
        mod = Bell202Modulator(
            sample_rate=self._cfg.sample_rate,
            baud=self._cfg.baud,
            mark_hz=self._cfg.mark_hz,
            space_hz=self._cfg.space_hz,
        )
        ptt = _build_ptt(self._cfg.ptt_device)

        def _push_block(block: bytes) -> None:
            try:
                queue.put_nowait(block)
            except asyncio.QueueFull:
                LOG.warning("afsk: audio queue overflow; dropping input block")

        def _audio_callback(indata, frames, time_info, status) -> None:
            if status:
                LOG.debug("afsk: sounddevice status=%s", status)
            loop.call_soon_threadsafe(_push_block, bytes(indata))

        blocksize = max(120, int(self._cfg.sample_rate / self._cfg.baud) * 12)
        input_device = self._cfg.input_device or None
        output_device = self._cfg.output_device or None
        input_stream = sounddevice.RawInputStream(
            samplerate=self._cfg.sample_rate,
            blocksize=blocksize,
            device=input_device,
            channels=1,
            dtype="int16",
            callback=_audio_callback,
        )
        output_stream = sounddevice.RawOutputStream(
            samplerate=self._cfg.sample_rate,
            blocksize=0,
            device=output_device,
            channels=1,
            dtype="int16",
        )

        LOG.info(
            "afsk: Bell 202 modem active input=%r output=%r sample_rate=%d baud=%d mark=%d space=%d ptt=%r",
            self._cfg.input_device or "<default>",
            self._cfg.output_device or "<default>",
            self._cfg.sample_rate,
            self._cfg.baud,
            self._cfg.mark_hz,
            self._cfg.space_hz,
            self._cfg.ptt_device or "<none>",
        )

        tx_task: asyncio.Task | None = None
        try:
            input_stream.start()
            output_stream.start()
            tx_task = asyncio.create_task(
                self._tx_loop(mod, output_stream, ptt),
                name="afsk-bell202-tx",
            )
            while True:
                block = await queue.get()
                for payload in demod.feed_samples(_pcm16le_to_float(block)):
                    await self._deliver_frame(payload)
        except asyncio.CancelledError:
            pass
        finally:
            if tx_task is not None:
                tx_task.cancel()
                with suppress(asyncio.CancelledError):
                    await tx_task
            await ptt.close()
            try:
                input_stream.stop()
            except Exception:
                pass
            try:
                input_stream.close()
            except Exception:
                pass
            try:
                output_stream.stop()
            except Exception:
                pass
            try:
                output_stream.close()
            except Exception:
                pass

    async def _deliver_frame(self, payload: bytes, port: int = 0) -> None:
        from ..ax25.frame import AX25Frame
        try:
            frame = AX25Frame.decode(payload)
            LOG.debug("afsk: RX port=%d %s", port, frame)
            await self._router.handle_frame(frame, port)
        except Exception as exc:
            LOG.debug("afsk: frame decode error: %s", exc)

    async def _tx_loop(self, mod: Bell202Modulator, output_stream, ptt: _PTTControl) -> None:
        while True:
            payload = await self._tx_queue.get()
            pcm = mod.modulate_ax25_frame(payload)
            try:
                await ptt.set_keyed(True)
                await asyncio.sleep(0.03)
                await asyncio.to_thread(output_stream.write, pcm)
                await asyncio.sleep(0.02)
            except Exception as exc:
                LOG.warning("afsk: TX error: %s", exc)
            finally:
                with suppress(Exception):
                    await ptt.set_keyed(False)


def _pcm16le_to_float(data: bytes) -> list[float]:
    samples = array("h")
    samples.frombytes(data)
    return [sample / 32768.0 for sample in samples]


def _levels_to_pcm16le(
    levels: Iterable[int],
    *,
    sample_rate: int,
    baud: int,
    mark_hz: int,
    space_hz: int,
    level: float,
) -> bytes:
    phase = 0.0
    samples_per_symbol = sample_rate / float(baud)
    frac = 0.0
    out = array("h")
    scale = int(32767 * level)
    for tone in levels:
        freq = mark_hz if tone else space_hz
        step = 2.0 * math.pi * freq / sample_rate
        frac += samples_per_symbol
        count = int(frac)
        if count <= 0:
            count = 1
        frac -= count
        for _ in range(count):
            out.append(int(scale * math.sin(phase)))
            phase += step
            if phase >= 2.0 * math.pi:
                phase -= 2.0 * math.pi
    return out.tobytes()


def _build_ptt(selector: str) -> _PTTControl:
    if not selector:
        return _NullPTT()
    kind, params = _parse_ptt_selector(selector)
    if kind == "serial_rts":
        return _SerialRTSPTT(str(params["device"]))
    if kind == "gpio":
        return _RPiGpioPTT(int(params["pin"]), active_high=bool(params["active_high"]))
    if kind == "gpiochip":
        return _GpiodPTT(
            str(params["chip"]),
            int(params["line"]),
            active_high=bool(params["active_high"]),
        )
    if kind == "cm108":
        return _CM108PTT(
            str(params["device"]),
            int(params["pin"]),
            active_high=bool(params["active_high"]),
        )
    LOG.warning("afsk: unsupported PTT selector %r; using no-op PTT", selector)
    return _NullPTT()


def _parse_ptt_selector(selector: str) -> tuple[str, dict[str, object]]:
    value = selector.strip()
    active_high = True
    if value.endswith(",active_low"):
        active_high = False
        value = value[:-11]
    elif value.endswith(",active_high"):
        value = value[:-12]

    if value.startswith("serial_rts:"):
        return "serial_rts", {"device": value.split(":", 1)[1], "active_high": active_high}
    if value.startswith("gpio:"):
        return "gpio", {"pin": int(value.split(":", 1)[1]), "active_high": active_high}
    if value.startswith("gpiochip:"):
        chip_line = value.split(":", 1)[1]
        chip, line = chip_line.rsplit(":", 1)
        return "gpiochip", {"chip": chip, "line": int(line), "active_high": active_high}
    if value.startswith("gpiochip") and ":" in value:
        chip, line = value.rsplit(":", 1)
        return "gpiochip", {"chip": chip, "line": int(line), "active_high": active_high}
    if value.startswith("cm108:"):
        device, pin = value.split(":", 1)[1].rsplit(":", 1)
        return "cm108", {"device": device, "pin": int(pin), "active_high": active_high}
    return "unknown", {"selector": selector}


def afsk_diagnostics(cfg: AfskConfig) -> list[str]:
    lines = [
        f"transport        : afsk",
        f"sample_rate      : {cfg.sample_rate}",
        f"baud             : {cfg.baud}",
        f"mark/space       : {cfg.mark_hz}/{cfg.space_hz}",
        f"input_device     : {cfg.input_device or '<default>'}",
        f"output_device    : {cfg.output_device or '<default>'}",
        f"ptt_device       : {cfg.ptt_device or '<none>'}",
    ]

    try:
        import sounddevice  # type: ignore[import]
    except ImportError:
        lines.append("sounddevice      : missing")
    else:
        lines.append("sounddevice      : available")
        try:
            devices = sounddevice.query_devices()
            lines.append(f"audio_devices    : {len(devices)} found")
            default_in, default_out = sounddevice.default.device
            lines.append(f"default_audio    : input={default_in} output={default_out}")
            for idx, dev in enumerate(devices):
                if idx >= 8:
                    lines.append("audio_list       : ...")
                    break
                lines.append(
                    "audio_list       : "
                    f"{idx}: {dev['name']} "
                    f"(in={dev['max_input_channels']} out={dev['max_output_channels']})"
                )
        except Exception as exc:
            lines.append(f"audio_devices    : error: {exc}")

    if not cfg.ptt_device:
        lines.append("ptt_backend      : none")
    else:
        kind, params = _parse_ptt_selector(cfg.ptt_device)
        lines.append(f"ptt_selector     : {kind}")
        if kind == "serial_rts":
            lines.append(f"ptt_target       : {params['device']}")
            try:
                import serial  # type: ignore[import]
            except ImportError:
                lines.append("ptt_support      : pyserial missing")
            else:
                lines.append("ptt_support      : pyserial available")
        elif kind == "gpio":
            lines.append(f"ptt_target       : BCM {params['pin']}")
            lines.append(f"ptt_polarity     : {'active_high' if params['active_high'] else 'active_low'}")
            try:
                import RPi.GPIO  # type: ignore[import]
            except ImportError:
                lines.append("ptt_support      : RPi.GPIO missing")
            else:
                lines.append("ptt_support      : RPi.GPIO available")
        elif kind == "gpiochip":
            lines.append(f"ptt_target       : {params['chip']} line {params['line']}")
            lines.append(f"ptt_polarity     : {'active_high' if params['active_high'] else 'active_low'}")
            try:
                import gpiod  # type: ignore[import]
            except ImportError:
                lines.append("ptt_support      : gpiod missing")
            else:
                lines.append("ptt_support      : gpiod available")
        elif kind == "cm108":
            lines.append(f"ptt_target       : {params['device']} gpio {params['pin']}")
            lines.append(f"ptt_polarity     : {'active_high' if params['active_high'] else 'active_low'}")
            lines.append("ptt_support      : hidraw write path")
        else:
            lines.append("ptt_support      : unknown selector")

    candidates = _find_cm108_hidraw_devices()
    if candidates:
        for candidate in candidates[:8]:
            lines.append(f"cm108_hidraw     : {candidate}")
        if len(candidates) > 8:
            lines.append("cm108_hidraw     : ...")
    else:
        lines.append("cm108_hidraw     : none found")

    lines.append("interface_notes  : SHARI / ARI / SignaLink / Masters Comm style soundcard interfaces can use AFSK")
    lines.append("interface_notes  : choose audio devices plus no-op, serial RTS, BCM GPIO, gpiochip, or CM108/119 HID PTT")
    return lines


def _find_cm108_hidraw_devices() -> list[str]:
    matches: list[str] = []
    for dev in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        uevent = dev / "device" / "uevent"
        hid_id = ""
        if uevent.exists():
            try:
                for line in uevent.read_text().splitlines():
                    if line.startswith("HID_ID="):
                        hid_id = line.split("=", 1)[1].strip().lower()
                        break
            except Exception:
                continue
        # USB HID bus 0003, vendor 0d8c (C-Media).
        if ":00000d8c:" not in f":{hid_id}:":
            continue
        matches.append(f"/dev/{dev.name}")
    return matches
