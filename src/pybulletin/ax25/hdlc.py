"""HDLC framing helpers for AX.25 over raw modem channels.

This sits below ``AX25Frame.encode()`` / ``decode()``.  AX.25 frame objects
represent the protocol payload without KISS or on-air HDLC encapsulation.

Bell 202, G3RUH, and similar modem implementations need these helpers to:
  - append / verify the AX.25 FCS (CRC-16/X.25, little-endian on the wire)
  - bit-stuff and unstuff the data stream
  - wrap and split frames with HDLC flag bytes (0x7E)
"""
from __future__ import annotations

from collections.abc import Iterable

FLAG = 0x7E
FLAG_BITS = (0, 1, 1, 1, 1, 1, 1, 0)  # 0x7E, LSB-first on the wire
_CRC_INIT = 0xFFFF
_CRC_XOROUT = 0xFFFF
_CRC_POLY_REVERSED = 0x8408

# Upper bound on bits we retain when no complete frame has been seen yet.
# Longest AX.25 frame is ~330 bytes (addresses + control + PID + paclen + FCS)
# with up to 20% bit-stuffing overhead, so ~3200 bits. 8192 leaves ample margin
# while keeping memory bounded on noise-only channels.
_MAX_REMAINDER_BITS = 8192


def crc_x25(data: bytes) -> int:
    """Return AX.25/HDLC CRC-16 (X.25/CCITT, reflected) for *data*."""
    crc = _CRC_INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ _CRC_POLY_REVERSED
            else:
                crc >>= 1
    return crc ^ _CRC_XOROUT


def append_fcs(payload: bytes) -> bytes:
    """Return *payload* with the AX.25 FCS appended little-endian."""
    crc = crc_x25(payload)
    return payload + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def verify_fcs(frame: bytes) -> bool:
    """Return True if *frame* ends with a valid AX.25 FCS."""
    if len(frame) < 3:
        return False
    payload = frame[:-2]
    got = frame[-2] | (frame[-1] << 8)
    return crc_x25(payload) == got


def bytes_to_bits_lsb(data: bytes) -> list[int]:
    """Convert bytes to LSB-first bit order as used by HDLC on the wire."""
    out: list[int] = []
    for byte in data:
        for bit in range(8):
            out.append((byte >> bit) & 0x01)
    return out


def bits_to_bytes_lsb(bits: Iterable[int]) -> bytes:
    """Pack an iterable of LSB-first bits into bytes."""
    out = bytearray()
    value = 0
    count = 0
    for bit in bits:
        if bit:
            value |= 1 << count
        count += 1
        if count == 8:
            out.append(value)
            value = 0
            count = 0
    if count:
        out.append(value)
    return bytes(out)


def bit_stuff(bits: Iterable[int]) -> list[int]:
    """Apply HDLC bit stuffing to *bits*."""
    out: list[int] = []
    ones = 0
    for bit in bits:
        out.append(bit)
        if bit:
            ones += 1
            if ones == 5:
                out.append(0)
                ones = 0
        else:
            ones = 0
    return out


def bit_unstuff(bits: Iterable[int]) -> list[int] | None:
    """Remove HDLC bit stuffing from *bits*.

    Returns None if an invalid stuffed sequence is encountered.
    """
    out: list[int] = []
    ones = 0
    skip_zero = False
    for bit in bits:
        if skip_zero:
            if bit != 0:
                return None
            skip_zero = False
            ones = 0
            continue
        out.append(bit)
        if bit:
            ones += 1
            if ones == 5:
                skip_zero = True
        else:
            ones = 0
    if skip_zero:
        return None
    return out


def nrzi_encode(bits: Iterable[int], *, initial: int = 1) -> list[int]:
    """Encode data bits to NRZI levels.

    AX.25 uses NRZI where data ``0`` causes a transition and data ``1``
    leaves the current level unchanged.
    """
    out: list[int] = []
    level = 1 if initial else 0
    for bit in bits:
        if bit == 0:
            level ^= 1
        out.append(level)
    return out


def nrzi_decode(levels: Iterable[int], *, initial: int = 1) -> list[int]:
    """Decode NRZI levels into data bits."""
    out: list[int] = []
    prev = 1 if initial else 0
    for level in levels:
        bit = 1 if level == prev else 0
        out.append(bit)
        prev = level
    return out


def encode_hdlc_frame(payload: bytes) -> bytes:
    """Encode one AX.25 payload into an HDLC-framed byte stream."""
    frame = append_fcs(payload)
    stuffed = bit_stuff(bytes_to_bits_lsb(frame))
    return bytes((FLAG,)) + bits_to_bytes_lsb(stuffed) + bytes((FLAG,))


def decode_hdlc_frame(frame: bytes) -> bytes:
    """Decode one HDLC-framed byte stream into AX.25 payload bytes."""
    if len(frame) < 4 or frame[0] != FLAG or frame[-1] != FLAG:
        raise ValueError("Missing HDLC frame flags")
    stuffed = bytes_to_bits_lsb(frame[1:-1])
    bits = bit_unstuff(stuffed)
    if bits is None:
        raise ValueError("Invalid HDLC bit-stuff sequence")
    raw = bits_to_bytes_lsb(bits)
    if not verify_fcs(raw):
        raise ValueError("Bad AX.25 FCS")
    return raw[:-2]


def extract_hdlc_frames(bits: list[int]) -> tuple[list[bytes], list[int]]:
    """Extract complete HDLC payloads from a raw bit stream.

    *bits* must be data bits after NRZI decoding but before bit unstuffing.
    Returns ``(payloads, remainder_bits)``.
    """
    frames: list[bytes] = []
    flag_positions: list[int] = []
    i = 0
    limit = len(bits) - len(FLAG_BITS)
    while i <= limit:
        if tuple(bits[i:i + 8]) == FLAG_BITS:
            flag_positions.append(i)
            i += 8
            continue
        i += 1

    if not flag_positions:
        # Pure noise with no flag ever seen. Keep only a tail short enough to
        # detect a flag that straddles the next buffer.
        if len(bits) > _MAX_REMAINDER_BITS:
            return frames, bits[-(len(FLAG_BITS) - 1):]
        return frames, bits

    if len(flag_positions) < 2:
        remainder = bits[flag_positions[0]:]
        if len(remainder) > _MAX_REMAINDER_BITS:
            # A single flag followed by far more bits than any valid frame
            # means we lost sync. Drop the stale flag and start fresh.
            return frames, bits[-(len(FLAG_BITS) - 1):]
        return frames, remainder

    for start, end in zip(flag_positions, flag_positions[1:]):
        stuffed = bits[start + 8:end]
        if not stuffed:
            continue
        unstuffed = bit_unstuff(stuffed)
        if unstuffed is None:
            continue
        raw = bits_to_bytes_lsb(unstuffed)
        if verify_fcs(raw):
            frames.append(raw[:-2])

    return frames, bits[flag_positions[-1]:]
