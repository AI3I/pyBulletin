from __future__ import annotations

from pybulletin.ax25.frame import AX25Address, AX25Frame
from pybulletin.ax25.hdlc import (
    FLAG,
    append_fcs,
    bit_stuff,
    bit_unstuff,
    bytes_to_bits_lsb,
    crc_x25,
    decode_hdlc_frame,
    encode_hdlc_frame,
    extract_hdlc_frames,
    nrzi_encode,
    verify_fcs,
)
from pybulletin.config import _build_afsk
from pybulletin.transport.afsk import (
    Bell202Demodulator,
    Bell202Modulator,
    _parse_ptt_selector,
    afsk_diagnostics,
)
import math


def test_crc_x25_known_value():
    assert crc_x25(b"123456789") == 0x906E


def test_append_and_verify_fcs():
    frame = append_fcs(b"hello")
    assert verify_fcs(frame) is True
    assert verify_fcs(frame[:-1] + bytes([frame[-1] ^ 0xFF])) is False


def test_bit_stuff_roundtrip():
    bits = [1, 1, 1, 1, 1, 0, 1, 0, 0, 1]
    stuffed = bit_stuff(bits)
    assert stuffed[:7] == [1, 1, 1, 1, 1, 0, 0]
    assert bit_unstuff(stuffed) == bits


def test_encode_decode_hdlc_frame_roundtrip():
    payload = AX25Frame.ui(
        dest=AX25Address("APBBS"),
        src=AX25Address("N0CALL"),
        info=b"bell202",
    ).encode()
    encoded = encode_hdlc_frame(payload)
    assert encoded[0] == FLAG
    assert encoded[-1] == FLAG
    assert decode_hdlc_frame(encoded) == payload


def test_decode_hdlc_frame_rejects_bad_fcs():
    payload = AX25Frame.ui(
        dest=AX25Address("APBBS"),
        src=AX25Address("N0CALL"),
        info=b"crc",
    ).encode()
    encoded = bytearray(encode_hdlc_frame(payload))
    encoded[-2] ^= 0x01
    try:
        decode_hdlc_frame(bytes(encoded))
    except ValueError as exc:
        assert "FCS" in str(exc)
    else:
        raise AssertionError("Expected bad FCS failure")


def test_extract_hdlc_frames_from_nrzi_decoded_bits():
    payload = AX25Frame.ui(
        dest=AX25Address("APBBS"),
        src=AX25Address("N0CALL"),
        info=b"rx",
    ).encode()
    frame = encode_hdlc_frame(payload)
    bits = bytes_to_bits_lsb(frame)
    decoded, remainder = extract_hdlc_frames(bits)
    assert decoded == [payload]
    assert remainder == list(bytes_to_bits_lsb(bytes([FLAG])))


def test_bell202_demodulator_recovers_ax25_payload():
    payload = AX25Frame.ui(
        dest=AX25Address("APBBS"),
        src=AX25Address("N0CALL"),
        info=b"bell202-rx",
    ).encode()
    bits = []
    for _ in range(4):
        bits.extend([0, 1, 1, 1, 1, 1, 1, 0])
    bits.extend(bytes_to_bits_lsb(encode_hdlc_frame(payload))[8:])
    levels = nrzi_encode(bits, initial=1)

    sample_rate = 48000
    baud = 1200
    samples_per_symbol = sample_rate // baud
    phase = 0.0
    waveform: list[float] = []
    for level in levels:
        freq = 1200 if level else 2200
        step = 2.0 * math.pi * freq / sample_rate
        for _ in range(samples_per_symbol):
            waveform.append(0.8 * math.sin(phase))
            phase += step

    demod = Bell202Demodulator(
        sample_rate=sample_rate,
        baud=baud,
        mark_hz=1200,
        space_hz=2200,
    )
    decoded = demod.feed_samples(waveform)
    assert payload in decoded


def test_bell202_modulator_demodulator_roundtrip():
    payload = AX25Frame.ui(
        dest=AX25Address("APBBS"),
        src=AX25Address("N0CALL"),
        info=b"bell202-txrx",
    ).encode()
    mod = Bell202Modulator(
        sample_rate=48000,
        baud=1200,
        mark_hz=1200,
        space_hz=2200,
        preamble_flags=24,
        postamble_flags=3,
    )
    demod = Bell202Demodulator(
        sample_rate=48000,
        baud=1200,
        mark_hz=1200,
        space_hz=2200,
    )
    decoded = demod.feed_samples([
        sample / 32768.0 for sample in array_from_pcm16le(mod.modulate_ax25_frame(payload))
    ])
    assert payload in decoded


def test_build_afsk_config():
    cfg = _build_afsk({
        "input_device": "hw:1,0",
        "output_device": "hw:1,0",
        "sample_rate": 44100,
        "baud": 1200,
        "mark_hz": 1200,
        "space_hz": 2200,
        "ptt_device": "gpio23",
        "dcd_enabled": False,
    })
    assert cfg.input_device == "hw:1,0"
    assert cfg.output_device == "hw:1,0"
    assert cfg.sample_rate == 44100
    assert cfg.baud == 1200
    assert cfg.mark_hz == 1200
    assert cfg.space_hz == 2200
    assert cfg.ptt_device == "gpio23"
    assert cfg.dcd_enabled is False


def test_build_kiss_transport_selector():
    from pybulletin.config import _build_kiss
    cfg = _build_kiss({
        "transport": "kiss_tcp",
        "tcp_host": "127.0.0.1",
        "tcp_port": 8001,
    })
    assert cfg.transport == "kiss_tcp"
    assert cfg.tcp_host == "127.0.0.1"
    assert cfg.tcp_port == 8001


def test_parse_ptt_selector_serial_rts():
    kind, params = _parse_ptt_selector("serial_rts:/dev/ttyUSB0")
    assert kind == "serial_rts"
    assert params["device"] == "/dev/ttyUSB0"
    assert params["active_high"] is True


def test_parse_ptt_selector_gpio_active_low():
    kind, params = _parse_ptt_selector("gpio:23,active_low")
    assert kind == "gpio"
    assert params["pin"] == 23
    assert params["active_high"] is False


def test_parse_ptt_selector_gpiochip():
    kind, params = _parse_ptt_selector("gpiochip:/dev/gpiochip0:24")
    assert kind == "gpiochip"
    assert params["chip"] == "/dev/gpiochip0"
    assert params["line"] == 24


def test_parse_ptt_selector_cm108():
    kind, params = _parse_ptt_selector("cm108:/dev/hidraw3:4,active_low")
    assert kind == "cm108"
    assert params["device"] == "/dev/hidraw3"
    assert params["pin"] == 4
    assert params["active_high"] is False


def test_afsk_diagnostics_basic_lines():
    cfg = _build_afsk({
        "input_device": "hw:1,0",
        "output_device": "hw:1,0",
        "ptt_device": "serial_rts:/dev/ttyUSB0",
    })
    lines = afsk_diagnostics(cfg)
    assert any("input_device" in line for line in lines)
    assert any("output_device" in line for line in lines)
    assert any("ptt_selector" in line for line in lines)


def array_from_pcm16le(data: bytes) -> list[int]:
    from array import array
    samples = array("h")
    samples.frombytes(data)
    return list(samples)
