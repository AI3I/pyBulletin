"""FBB version 0 compressed ASCII message framing (FA proposals)."""
from __future__ import annotations

import asyncio

from .protocol import lzhuf_compress, lzhuf_decompress, _HAVE_LZHUF

_MAX_COMPRESSED = 512_000


def encode_fa(subject: str, body: str) -> bytes:
    if not _HAVE_LZHUF:
        raise RuntimeError("FBB compressed forwarding requires LZHUF")
    title = subject.replace("\r", " ").replace("\n", " ").encode("ascii", "replace")[:60]
    payload = body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n").encode("ascii", "replace")
    if not payload.endswith(b"\r\n"):
        payload += b"\r\n"
    compressed = lzhuf_compress(payload)
    if len(compressed) > _MAX_COMPRESSED:
        raise ValueError("compressed FBB message is too large")
    header = b"\x01" + bytes([len(title) + 8]) + title + b"\x00     0\x00"
    blocks = b"".join(b"\x02" + bytes([len(part)]) + part
                      for part in (compressed[i:i + 250] for i in range(0, len(compressed), 250)))
    return header + blocks + b"\x04" + bytes([-sum(compressed) & 0xff])


async def decode_fa(reader, *, max_body: int = 256_000) -> tuple[str, str]:
    async def exact(count: int) -> bytes:
        return await asyncio.wait_for(reader.readexactly(count), 60)

    if await exact(1) != b"\x01":
        raise ValueError("invalid FBB binary header")
    length = (await exact(1))[0]
    if not 3 <= length <= 90:
        raise ValueError("invalid FBB binary header length")
    header = await exact(length)
    parts = header.split(b"\x00")
    if len(parts) < 3:
        raise ValueError("invalid FBB binary header fields")
    subject = parts[0].decode("ascii", "replace")
    try:
        offset = int(parts[1].strip() or b"0")
    except ValueError as exc:
        raise ValueError("invalid FBB binary restart offset") from exc
    if offset:
        raise ValueError("FBB B0 restart offsets are unsupported")
    compressed = bytearray()
    while True:
        marker = await exact(1)
        if marker == b"\x04":
            checksum = (await exact(1))[0]
            if (sum(compressed) + checksum) & 0xff:
                raise ValueError("FBB binary checksum mismatch")
            break
        if marker != b"\x02":
            raise ValueError("invalid FBB binary block marker")
        size = (await exact(1))[0] or 256
        compressed.extend(await exact(size))
        if len(compressed) > _MAX_COMPRESSED:
            raise ValueError("compressed FBB message is too large")
    if not _HAVE_LZHUF:
        raise RuntimeError("FBB compressed forwarding requires LZHUF")
    if len(compressed) < 4 or int.from_bytes(compressed[:4], "little") > max_body:
        raise ValueError("incoming FBB message exceeds forwarding limit")
    text = lzhuf_decompress(bytes(compressed)).decode("ascii", "replace")
    return subject, text.replace("\r\n", "\n").rstrip("\n")
