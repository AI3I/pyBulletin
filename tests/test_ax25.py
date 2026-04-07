"""Tests for AX.25 frame encode/decode, KISS framing, and connection state."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pybulletin.ax25.frame import (
    AX25Address, AX25Frame, FrameType,
    PID_NO_L3, _CTRL_UI, _CTRL_SABM, _CTRL_UA, _CTRL_DISC, _CTRL_DM,
)
from pybulletin.ax25.connection import AX25Connection, ConnState
from pybulletin.transport.kiss import encode, decode_stream, encode_cmd


# ---------------------------------------------------------------------------
# AX25Address
# ---------------------------------------------------------------------------

def test_address_parse_no_ssid():
    a = AX25Address.parse("W3BBS")
    assert a.callsign == "W3BBS"
    assert a.ssid == 0


def test_address_parse_with_ssid():
    a = AX25Address.parse("W3BBS-7")
    assert a.callsign == "W3BBS"
    assert a.ssid == 7


def test_address_str_no_ssid():
    a = AX25Address(callsign="W3BBS", ssid=0)
    assert str(a) == "W3BBS"


def test_address_str_with_ssid():
    a = AX25Address(callsign="W3BBS", ssid=3)
    assert str(a) == "W3BBS-3"


def test_address_encode_decode_roundtrip():
    orig = AX25Address(callsign="AI3I", ssid=14, ch=True, end=True)
    data = orig.encode()
    assert len(data) == 7
    decoded = AX25Address.decode(data)
    assert decoded.callsign == "AI3I"
    assert decoded.ssid == 14
    assert decoded.ch is True
    assert decoded.end is True


def test_address_matches():
    a = AX25Address.parse("W3BBS-2")
    b = AX25Address(callsign="W3BBS", ssid=2, ch=True)
    assert a.matches(b)


def test_address_not_matches_ssid():
    a = AX25Address.parse("W3BBS-2")
    b = AX25Address.parse("W3BBS-3")
    assert not a.matches(b)


# ---------------------------------------------------------------------------
# AX25Frame encode/decode roundtrip
# ---------------------------------------------------------------------------

def _make_frame(dest="W3BBS", src="AI3I", info=b"Hello", ssid_d=0, ssid_s=0):
    return AX25Frame.ui(
        dest=AX25Address(dest, ssid_d),
        src=AX25Address(src, ssid_s),
        info=info,
    )


def test_ui_frame_roundtrip():
    frame = _make_frame(info=b"test payload")
    encoded = frame.encode()
    decoded = AX25Frame.decode(encoded)
    assert decoded.frame_type == FrameType.UI
    assert decoded.info == b"test payload"
    assert decoded.dest.callsign == "W3BBS"
    assert decoded.src.callsign == "AI3I"


def test_ui_frame_pid():
    frame = _make_frame()
    assert frame.pid == PID_NO_L3


def test_sabm_frame_type():
    frame = AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I"))
    assert frame.frame_type == FrameType.SABM


def test_ua_frame_type():
    frame = AX25Frame.ua(AX25Address("W3BBS"), AX25Address("AI3I"))
    assert frame.frame_type == FrameType.UA


def test_disc_frame_type():
    frame = AX25Frame.disc(AX25Address("W3BBS"), AX25Address("AI3I"))
    assert frame.frame_type == FrameType.DISC


def test_dm_frame_type():
    frame = AX25Frame.dm(AX25Address("W3BBS"), AX25Address("AI3I"))
    assert frame.frame_type == FrameType.DM


def test_iframe_roundtrip():
    frame = AX25Frame.iframe(
        dest=AX25Address("W3BBS"),
        src=AX25Address("AI3I"),
        ns=3, nr=5,
        info=b"data",
    )
    assert frame.frame_type == FrameType.I
    assert frame.ns == 3
    assert frame.nr == 5
    encoded = frame.encode()
    decoded = AX25Frame.decode(encoded)
    assert decoded.frame_type == FrameType.I
    assert decoded.info == b"data"


def test_rr_frame():
    frame = AX25Frame.rr(AX25Address("W3BBS"), AX25Address("AI3I"), nr=4)
    assert frame.frame_type == FrameType.RR
    assert frame.nr == 4


def test_rej_frame():
    frame = AX25Frame.rej(AX25Address("W3BBS"), AX25Address("AI3I"), nr=2)
    assert frame.frame_type == FrameType.REJ
    assert frame.nr == 2


def test_frame_with_repeaters():
    reps = [AX25Address("RELAY1"), AX25Address("RELAY2")]
    frame = _make_frame()
    frame.repeaters = reps
    encoded = frame.encode()
    decoded = AX25Frame.decode(encoded)
    assert len(decoded.repeaters) == 2
    assert decoded.repeaters[0].callsign == "RELAY1"


def test_decode_too_short_raises():
    with pytest.raises(ValueError):
        AX25Frame.decode(b"\x00" * 5)


# ---------------------------------------------------------------------------
# KISS framing
# ---------------------------------------------------------------------------

def test_kiss_encode_decode_roundtrip():
    payload = b"\x00\x01\x02\xC0\xDB\xFF"  # includes special bytes
    framed = encode(payload, port=0)
    buf = bytearray(framed)
    frames = list(decode_stream(buf))
    assert len(frames) == 1
    port, cmd, data = frames[0]
    assert port == 0
    assert cmd == 0
    assert data == payload


def test_kiss_port_encoding():
    framed = encode(b"data", port=3)
    buf = bytearray(framed)
    frames = list(decode_stream(buf))
    assert frames[0][0] == 3


def test_kiss_fend_escaped():
    payload = bytes([0xC0])  # FEND byte must be escaped
    framed = encode(payload)
    # Should contain FESC TFEND (0xDB 0xDC) not a raw FEND in data
    inner = framed[2:-1]  # strip leading FEND+type and trailing FEND
    assert 0xC0 not in inner


def test_kiss_fesc_escaped():
    payload = bytes([0xDB])  # FESC byte must be escaped
    framed = encode(payload)
    inner = framed[2:-1]
    assert inner == bytes([0xDB, 0xDD])


def test_kiss_multiple_frames():
    buf = bytearray()
    buf += encode(b"frame1")
    buf += encode(b"frame2")
    frames = list(decode_stream(buf))
    assert len(frames) == 2
    assert frames[0][2] == b"frame1"
    assert frames[1][2] == b"frame2"


def test_kiss_incomplete_frame_buffered():
    partial = bytearray(encode(b"hello")[:-1])  # missing trailing FEND
    frames = list(decode_stream(partial))
    assert frames == []
    assert len(partial) > 0  # not consumed


def test_kiss_encode_cmd():
    frame = encode_cmd(0x01, 20)  # TX delay = 20
    assert frame[0] == 0xC0
    assert frame[-1] == 0xC0
    assert frame[2] == 20


# ---------------------------------------------------------------------------
# AX25Connection state machine
# ---------------------------------------------------------------------------

def _make_connection(local="W3BBS", remote="AI3I"):
    sent_frames = []

    async def _send(frame):
        sent_frames.append(frame)

    conn = AX25Connection(
        local_addr=AX25Address.parse(local),
        remote_addr=AX25Address.parse(remote),
        send_frame_cb=_send,
    )
    return conn, sent_frames


async def test_handle_sabm_sends_ua_and_connected():
    conn, sent = _make_connection()
    sabm = AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I"))
    await conn.handle_frame(sabm)
    assert conn.state == ConnState.CONNECTED
    assert any(f.frame_type == FrameType.UA for f in sent)


async def test_handle_disc_sends_ua_and_disconnects():
    conn, sent = _make_connection()
    # First connect
    await conn.handle_frame(AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I")))
    assert conn.state == ConnState.CONNECTED

    # Then disconnect
    await conn.handle_frame(AX25Frame.disc(AX25Address("W3BBS"), AX25Address("AI3I")))
    assert conn.state == ConnState.DISCONNECTED


async def test_handle_dm_disconnects():
    conn, sent = _make_connection()
    await conn.handle_frame(AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I")))
    await conn.handle_frame(AX25Frame.dm(AX25Address("W3BBS"), AX25Address("AI3I")))
    assert conn.state == ConnState.DISCONNECTED


async def test_write_and_read_iframe():
    conn, sent = _make_connection()
    await conn.handle_frame(AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I")))

    await conn.write(b"hello")
    # Should have sent an I-frame
    iframes = [f for f in sent if f.frame_type == FrameType.I]
    assert len(iframes) == 1
    assert iframes[0].info == b"hello"


async def test_receive_iframe_delivered_to_rx_queue():
    conn, sent = _make_connection()
    await conn.handle_frame(AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I")))

    iframe = AX25Frame.iframe(
        AX25Address("W3BBS"), AX25Address("AI3I"),
        ns=0, nr=0, info=b"received data",
    )
    await conn.handle_frame(iframe)

    data = await asyncio.wait_for(conn.read(), timeout=1.0)
    assert data == b"received data"


async def test_disconnect_wakes_blocked_reader():
    """Disconnecting should put sentinel into rx_queue so read() unblocks."""
    conn, sent = _make_connection()
    await conn.handle_frame(AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I")))

    async def _wait_for_data():
        return await conn.read()

    task = asyncio.create_task(_wait_for_data())
    await asyncio.sleep(0)  # let task start waiting

    await conn.handle_frame(AX25Frame.disc(AX25Address("W3BBS"), AX25Address("AI3I")))

    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == b""  # sentinel


async def test_window_limit_respected():
    """Should not send more than _WINDOW (7) unacked I-frames."""
    conn, sent = _make_connection()
    await conn.handle_frame(AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I")))

    # Write 10 chunks — only 7 should go out immediately
    for i in range(10):
        await conn.write(f"chunk{i}".encode())

    iframes = [f for f in sent if f.frame_type == FrameType.I]
    assert len(iframes) <= 7


async def test_rej_triggers_retransmit():
    conn, sent = _make_connection()
    await conn.handle_frame(AX25Frame.sabm(AX25Address("W3BBS"), AX25Address("AI3I")))
    await conn.write(b"data")
    before = len([f for f in sent if f.frame_type == FrameType.I])

    rej = AX25Frame.rej(AX25Address("W3BBS"), AX25Address("AI3I"), nr=0)
    await conn.handle_frame(rej)

    after = len([f for f in sent if f.frame_type == FrameType.I])
    assert after > before  # retransmit happened
