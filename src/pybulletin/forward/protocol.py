"""FBB B2F forwarding protocol — wire encoding and decoding.

Implements the proposal/response handshake and B2F binary message blocks
used between FBB-compatible BBS nodes.

Proposal line formats
---------------------
Outgoing (we want to send a message)::

    FB <to> <from> <bid> <size> <date> <title>\\r\\n

Incoming response to our proposals (one char per proposal)::

    FS <+|-|=><+|-|=>...\\r\\n

    +  accept — send it
    -  reject — don't send
    =  duplicate — already have it

Remote sends its own outgoing proposals (messages for us)::

    FB <to> <from> <bid> <size> <date> <title>\\r\\n
    ...
    FF\\r\\n   (no more proposals)

We respond with FS line, then receive the accepted messages.

End-of-session::

    FQ\\r\\n   (quit — end forwarding session)

B2F binary message block
-------------------------
After proposal acceptance, each message is sent as::

    F> <compressed_size>\\r\\n
    <compressed_size bytes of LZHUF data>

The LZHUF data decompresses to a message envelope::

    Mid: <bid>\\r\\n
    Date: <date_string>\\r\\n
    From: <from_call>\\r\\n
    To: <to_call>\\r\\n
    @ <at_bbs>\\r\\n       (optional)
    Subject: <subject>\\r\\n
    Mbo: <originating_bbs>\\r\\n  (optional)
    Type: P|B|T\\r\\n
    Body: <body_byte_count>\\r\\n
    \\r\\n
    <body_bytes>
    \\x03

B1 text message (fallback for nodes that don't support B2F)
------------------------------------------------------------
::

    F+ <size>\\r\\n
    <size bytes of plain message text>

    Message text format::
        From: <from_call>\\r\\n
        To: <to_call>\\r\\n
        Subject: <subject>\\r\\n
        Date: <date>\\r\\n
        ---\\r\\n
        <body>\\r\\n
        /EX\\r\\n
"""
from __future__ import annotations

import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

# Vendor LZHUF
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "vendor"))
try:
    from lzhuf import compress as lzhuf_compress, decompress as lzhuf_decompress
    _HAVE_LZHUF = True
except ImportError:
    _HAVE_LZHUF = False

from ..store.models import Message, MSG_PRIVATE, MSG_BULLETIN, MSG_NTS, STATUS_NEW

CRLF = b"\r\n"
_DATE_FMT = "%Y/%m/%d %H:%M"


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

@dataclass
class Proposal:
    """One FA/FB forwarding proposal."""
    to_call:  str
    from_call: str
    bid:      str
    size:     int
    date:     str = ""
    subject:  str = ""
    # Set after the remote responds to our proposal
    accepted: bool = False


def format_proposal(msg: Message, *, binary: bool = True) -> str:
    """Format a message as a B2F (FB) or B1 (FA) proposal line."""
    date_str = msg.created_at.strftime(_DATE_FMT) if msg.created_at else \
               datetime.now(timezone.utc).strftime(_DATE_FMT)
    prefix = "FB" if binary else "FA"
    return (f"{prefix} {msg.to_call} {msg.from_call} {msg.bid} "
            f"{msg.size} {date_str} {msg.subject}")


def parse_proposal(line: str) -> Proposal | None:
    """Parse an FB or FA proposal line from the remote."""
    line = line.strip()
    if not (line.startswith("FB ") or line.startswith("FA ")):
        return None
    parts = line[3:].split(None, 5)
    if len(parts) < 4:
        return None
    to_call   = parts[0].upper()
    from_call = parts[1].upper()
    bid       = parts[2]
    try:
        size = int(parts[3])
    except ValueError:
        return None
    date    = parts[4] if len(parts) > 4 else ""
    subject = parts[5] if len(parts) > 5 else ""
    return Proposal(
        to_call=to_call, from_call=from_call,
        bid=bid, size=size, date=date, subject=subject,
    )


def parse_fs_response(line: str) -> list[str]:
    """Parse an FS response line into a list of response characters."""
    line = line.strip()
    if not line.startswith("FS "):
        return []
    return list(line[3:].strip())


# ---------------------------------------------------------------------------
# B2F message serialisation
# ---------------------------------------------------------------------------

def encode_message_b2f(msg: Message) -> bytes:
    """Encode a Message as a B2F binary block (compressed).

    Returns ``F> <size>\\r\\n<compressed_data>`` ready to write to wire.
    """
    envelope = _build_envelope(msg)
    if _HAVE_LZHUF:
        compressed = lzhuf_compress(envelope)
    else:
        # Fallback: send uncompressed with a 4-byte LE size prefix
        compressed = struct.pack("<I", len(envelope)) + envelope
    return f"F> {len(compressed)}\r\n".encode() + compressed


def encode_message_b1(msg: Message) -> bytes:
    """Encode a Message as a B1 text block.

    Returns ``F+ <size>\\r\\n<text_data>`` ready to write to wire.
    """
    lines = [
        f"From: {msg.from_call}",
        f"To: {msg.to_call}",
    ]
    if msg.at_bbs:
        lines.append(f"@ {msg.at_bbs}")
    lines += [
        f"Subject: {msg.subject}",
        f"Date: {msg.created_at.strftime(_DATE_FMT) if msg.created_at else ''}",
        f"BID: {msg.bid}",
        f"Type: {msg.msg_type}",
        "---",
        msg.body,
        "/EX",
    ]
    text = "\r\n".join(lines).encode("ascii", errors="replace") + b"\r\n"
    return f"F+ {len(text)}\r\n".encode() + text


def _build_envelope(msg: Message) -> bytes:
    """Build the uncompressed message envelope for B2F."""
    date_str = msg.created_at.strftime(_DATE_FMT) if msg.created_at else \
               datetime.now(timezone.utc).strftime(_DATE_FMT)
    body_bytes = msg.body.encode("ascii", errors="replace")
    lines = [
        f"Mid: {msg.bid}",
        f"Date: {date_str}",
        f"From: {msg.from_call}",
        f"To: {msg.to_call}",
    ]
    if msg.at_bbs:
        lines.append(f"@ {msg.at_bbs}")
    lines += [
        f"Subject: {msg.subject}",
        f"Type: {msg.msg_type}",
        f"Body: {len(body_bytes)}",
        "",
    ]
    header = "\r\n".join(lines).encode("ascii", errors="replace") + b"\r\n"
    return header + body_bytes + b"\x03"


# ---------------------------------------------------------------------------
# B2F / B1 message decoding
# ---------------------------------------------------------------------------

def decode_b2f_block(data: bytes) -> Message | None:
    """Decode a B2F binary block (already decompressed) into a Message."""
    text = data.decode("ascii", errors="replace")
    # Strip trailing ctrl-Z or /EX
    text = text.rstrip("\x03\x1a").strip()

    headers: dict[str, str] = {}
    body_start = 0

    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped == "" or stripped == "---":
            body_start = i + 1
            break
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            headers[key.strip().lower()] = val.strip()

    lines = text.splitlines()
    body_text = "\n".join(lines[body_start:])

    # body_len header tells us how many bytes; trim to that if present
    if "body" in headers:
        try:
            body_len = int(headers["body"])
            body_text = body_text[:body_len]
        except ValueError:
            pass

    bid      = headers.get("mid", headers.get("bid", ""))
    from_c   = headers.get("from", "").upper()
    to_c     = headers.get("to",   "").upper()
    at_bbs   = (headers.get("@") or headers.get("at") or "").upper()
    subject  = headers.get("subject", "(no subject)")
    msg_type = headers.get("type", MSG_PRIVATE).upper()
    if msg_type not in (MSG_PRIVATE, MSG_BULLETIN, MSG_NTS):
        msg_type = MSG_PRIVATE

    return Message(
        bid=bid,
        msg_type=msg_type,
        status=STATUS_NEW,
        from_call=from_c,
        to_call=to_c,
        at_bbs=at_bbs,
        subject=subject,
        body=body_text.strip(),
        created_at=datetime.now(timezone.utc),
    )


def decode_b1_block(data: bytes) -> Message | None:
    """Decode a B1 text block into a Message."""
    return decode_b2f_block(data)  # format is similar enough


def decompress_b2f(data: bytes) -> bytes | None:
    """Decompress a B2F binary payload.

    The first 4 bytes are the original (uncompressed) size in little-endian.
    Returns None on failure.
    """
    if len(data) < 4:
        return None
    original_size = struct.unpack_from("<I", data, 0)[0]
    if not _HAVE_LZHUF:
        # No compressor — assume data wasn't actually compressed
        return data[4:4 + original_size]
    try:
        return lzhuf_decompress(data, original_size)
    except Exception:
        return None
