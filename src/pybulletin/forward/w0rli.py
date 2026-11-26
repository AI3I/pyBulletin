"""Classic W0RLI/Kantronics text forwarding (HM$), separate from FBB.

The KPC-9612 ROM 8.2 internal PBBS confirmed the SID, short prompt,
SP/OK/body/prompt, duplicate NO, and reverse F>/message/DONE exchanges.
The session flow also follows the 1993 BBS Interface Specification draft.
"""
from __future__ import annotations

import re

from .. import __version__
from ..store.models import Message, MSG_BULLETIN, MSG_NTS, MSG_PRIVATE

_SEND = re.compile(
    r"S([PBT])\s+(\S+)(?:\s+@\s+(\S+))?"
    r"(?:\s+<\s+(\S+))?(?:\s+\$(\S+))?\s*\Z", re.I,
)
_MAX_BODY = 256_000


def _sid(text: str) -> bool:
    """Recognize the last (feature) field of a classic BBS SID."""
    if not text.startswith("[") or not text.endswith("]"):
        return False
    fields = text[1:-1].rsplit("-", 1)
    return len(fields) == 2 and "H" in fields[1].upper() and "$" in fields[1]


def _send_command(msg: Message) -> str:
    if msg.bid and len(msg.bid) > 12:
        raise ValueError("classic forwarding BID exceeds 12 characters")
    kind = msg.msg_type if msg.msg_type in (MSG_PRIVATE, MSG_BULLETIN, MSG_NTS) else MSG_PRIVATE
    line = f"S{kind} {msg.to_call}"
    if msg.at_bbs:
        line += f" @ {msg.at_bbs}"
    line += f" < {msg.from_call}"
    if msg.bid and kind != MSG_NTS:
        line += f" ${msg.bid}"
    return line


def _parse_send(line: str, peer_call: str) -> Message | None:
    match = _SEND.fullmatch(line)
    if not match:
        return None
    kind, to_call, at_bbs, from_call, bid = match.groups()
    if bid and len(bid) > 12:
        return None
    return Message(
        msg_type=kind.upper(), to_call=to_call.upper(),
        at_bbs=(at_bbs or "").upper(), from_call=(from_call or peer_call).upper(),
        bid=(bid or "").upper(),
    )


async def _prompt(session) -> None:
    for _ in range(30):
        if (await session._readline()).strip().endswith(">"):
            return
    raise ConnectionError("classic forwarding prompt not received")


async def _response(session) -> bool:
    for _ in range(30):
        line = (await session._readline()).strip().upper()
        if line == "OK" or line.startswith("OK "):
            return True
        if line == "NO" or line.startswith("NO "):
            return False
    raise ConnectionError("classic forwarding acceptance response not received")


async def _write_body(session, msg: Message) -> None:
    if "\x1a" in msg.subject + msg.body:
        raise ValueError("classic forwarding message contains an end marker")
    if len(msg.subject) > 79 or len(msg.body.encode("utf-8")) > _MAX_BODY:
        raise ValueError("classic forwarding message exceeds size limit")
    payload = msg.subject.replace("\r", " ").replace("\n", " ") + "\r"
    payload += msg.body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
    payload += "\r\x1a\r"
    session._writer.write(payload.encode("ascii", errors="replace"))
    await session._writer.drain()


async def _read_body(session, msg: Message) -> None:
    msg.subject = await session._readline()
    lines = []
    size = 0
    while True:
        line = await session._readline()
        if line == "\x1a":
            break
        if "\x1a" in line:
            raise ValueError("unexpected classic forwarding end marker")
        size += len(line.encode("ascii", errors="replace")) + 2
        if size > _MAX_BODY:
            raise ValueError("classic forwarding message exceeds size limit")
        lines.append(line)
    msg.body = "\n".join(lines)


async def _pending(session, attempted: set[int]) -> list[Message]:
    result = []
    for _ in range(1000):
        batch = await session._select_outgoing(attempted)
        if not batch:
            break
        attempted.update(msg.id for msg in batch)
        result.extend(batch)
    else:
        raise ConnectionError("classic forwarding exceeded message limit")
    return result


async def _send_as_master(session) -> None:
    for msg in await _pending(session, set()):
        await session._writeline(_send_command(msg))
        accepted = await _response(session)
        if accepted:
            await _write_body(session, msg)
        await _prompt(session)
        if accepted:
            await session._store.mark_delivered(msg.id, session._neighbor.call)
            session._sent += 1


async def _receive_as_master(session) -> None:
    for _ in range(1000):
        line = (await session._readline()).strip()
        if line.upper().startswith("*** DONE"):
            await _prompt(session)
            return
        if line.endswith(">") or not line:
            continue
        msg = _parse_send(line, session._neighbor.call)
        if msg is None:
            raise ConnectionError(f"unexpected classic reverse proposal: {line[:80]!r}")
        if msg.bid and await session._store.has_bid(msg.bid):
            await session._writeline("NO")
        else:
            await session._writeline("OK")
            await _read_body(session, msg)
            msg.id = await session._store.insert_message(msg)
            await session._store.append_forward_path(msg.id, session._neighbor.call)
            session._received += 1
        await session._writeline("F>")
    raise ConnectionError("classic reverse forwarding exceeded message limit")


async def _receive_as_slave(session) -> None:
    for _ in range(1000):
        line = (await session._readline()).strip()
        if line == "F>":
            return
        msg = _parse_send(line, session._neighbor.call)
        if msg is None:
            raise ConnectionError(f"unexpected classic forward proposal: {line[:80]!r}")
        if msg.bid and await session._store.has_bid(msg.bid):
            await session._writeline("NO")
        else:
            await session._writeline("OK")
            await _read_body(session, msg)
            msg.id = await session._store.insert_message(msg)
            await session._store.append_forward_path(msg.id, session._neighbor.call)
            session._received += 1
        await session._writeline(">")
    raise ConnectionError("classic forwarding exceeded message limit")


async def _send_as_slave(session) -> None:
    for msg in await _pending(session, set()):
        await session._writeline(_send_command(msg))
        accepted = await _response(session)
        if accepted:
            await _write_body(session, msg)
        # The master acknowledges the completed body or rejection with F>.
        if (await session._readline()).strip() != "F>":
            raise ConnectionError("classic reverse forwarding acknowledgement missing")
        if accepted:
            await session._store.mark_delivered(msg.id, session._neighbor.call)
            session._sent += 1
    await session._writeline("*** DONE")
    await session._writeline(">")


async def run(session, *, caller: bool, greeted: bool = False) -> None:
    """Exchange classic mail on the connected ForwardSession transport."""
    our_sid = f"[{session._cfg.node.node_call}-pyBulletin{__version__}-HM$]"
    if not caller and not greeted:
        await session._writeline(our_sid)
        await session._writeline(">")
    for _ in range(30):
        remote_line = (await session._readline()).strip()
        if remote_line.startswith("["):
            if not _sid(remote_line):
                raise ConnectionError("remote SID does not offer classic hierarchical forwarding")
            break
    else:
        raise ConnectionError("classic forwarding SID not received")
    if caller:
        await _prompt(session)
        await session._writeline(our_sid)
        await _prompt(session)
        await _send_as_master(session)
        await session._writeline("F>")
        await _receive_as_master(session)
    else:
        await session._writeline(">")
        await _receive_as_slave(session)
        await _send_as_slave(session)
