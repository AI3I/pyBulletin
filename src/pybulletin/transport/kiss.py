"""KISS (Keep It Simple Stupid) TNC framing.

Implements the KISS protocol as defined in the ARRL 1997 standard.
This module is pure encode/decode — no I/O.

Frame structure on the wire:
    FEND | TYPE | <escaped AX.25 data> | FEND

TYPE byte = (port << 4) | command
  port    0-7  — TNC port number (0 for most single-port TNCs)
  command 0x00 — data frame
  command 0x01 — TX delay  (data = 10 ms units)
  command 0x02 — persistence
  command 0x03 — slot time
  command 0x04 — TX tail
  command 0x05 — full duplex flag
  command 0x06 — set hardware (TNC-specific)
  command 0xFF — exit KISS mode

Escaping inside the data field:
    0xC0 → 0xDB 0xDC   (FESC TFEND)
    0xDB → 0xDB 0xDD   (FESC TFESC)
"""
from __future__ import annotations

from typing import Generator

# Special bytes
FEND  = 0xC0   # Frame End — marks frame boundaries
FESC  = 0xDB   # Frame Escape
TFEND = 0xDC   # Transposed FEND (follows FESC)
TFESC = 0xDD   # Transposed FESC (follows FESC)

# KISS command codes (lower nibble of type byte)
CMD_DATA      = 0x00
CMD_TXDELAY   = 0x01
CMD_PERSIST   = 0x02
CMD_SLOTTIME  = 0x03
CMD_TXTAIL    = 0x04
CMD_FULLDUPLEX = 0x05
CMD_SETHW     = 0x06
CMD_EXIT_KISS = 0xFF


def encode(data: bytes, port: int = 0, cmd: int = CMD_DATA) -> bytes:
    """Encode *data* as a KISS frame for *port*.

    Returns the complete framed bytes including leading and trailing FEND.
    """
    escaped = bytearray()
    for b in data:
        if b == FEND:
            escaped += bytes([FESC, TFEND])
        elif b == FESC:
            escaped += bytes([FESC, TFESC])
        else:
            escaped.append(b)
    type_byte = ((port & 0x0F) << 4) | (cmd & 0x0F)
    return bytes([FEND, type_byte]) + bytes(escaped) + bytes([FEND])


def decode_stream(
    buf: bytearray,
) -> Generator[tuple[int, int, bytes], None, None]:
    """Decode and *consume* complete KISS frames from *buf*.

    Yields ``(port, cmd, data)`` for each complete frame found.
    Consumed bytes are removed from *buf* in-place.  Incomplete frames
    at the end of *buf* are left intact.
    """
    while True:
        # Find frame start
        try:
            start = buf.index(FEND)
        except ValueError:
            break  # no FEND at all — wait for more data

        # Skip any leading FENDs (inter-frame gap)
        i = start
        while i < len(buf) and buf[i] == FEND:
            i += 1

        if i >= len(buf):
            # Only FENDs in buffer
            del buf[:i]
            break

        # i now points to the type byte; find the terminating FEND
        try:
            end = buf.index(FEND, i)
        except ValueError:
            # Frame not complete yet — discard leading FENDs and wait
            del buf[:i]
            break

        # Extract and unescape the payload
        type_byte = buf[i]
        raw       = buf[i + 1:end]
        del buf[:end + 1]  # consume through terminating FEND

        port = (type_byte >> 4) & 0x0F
        cmd  = type_byte & 0x0F

        data = _unescape(raw)
        if data is not None:
            yield port, cmd, data
        # if data is None the frame had a bad escape sequence — silently drop


def _unescape(raw: bytes | bytearray) -> bytes | None:
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == FESC:
            i += 1
            if i >= len(raw):
                return None  # truncated escape
            nb = raw[i]
            if nb == TFEND:
                out.append(FEND)
            elif nb == TFESC:
                out.append(FESC)
            else:
                return None  # invalid escape sequence
        elif b == FEND:
            return None  # unexpected FEND inside frame
        else:
            out.append(b)
        i += 1
    return bytes(out)


def encode_cmd(cmd: int, value: int, port: int = 0) -> bytes:
    """Encode a KISS parameter-set command (TX delay, etc.)."""
    return bytes([FEND, ((port & 0x0F) << 4) | (cmd & 0x0F), value & 0xFF, FEND])
