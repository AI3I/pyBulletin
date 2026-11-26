"""Classic forwarding against the KPC-9612's observed text dialogue."""
from __future__ import annotations

import asyncio
import re

import pytest

from pybulletin.config import AppConfig, ForwardNeighborConfig
from pybulletin.forward.session import ForwardSession, _WireReader
from pybulletin.forward.w0rli import _parse_send, _routing_body, _send_command, _sid
from pybulletin.store.models import Message, MSG_BULLETIN, MSG_PRIVATE
from pybulletin.store.store import BBSStore


async def _line(wire):
    return (await wire.readline()).decode("ascii").strip()


async def _body(wire):
    subject = await _line(wire)
    lines = []
    while (line := await _line(wire)) != "\x1a":
        lines.append(line)
    return subject, "\n".join(lines).strip()


def test_classic_proposal_roundtrip():
    assert _sid("[KPC9612-8.2-HM$]")
    assert not _sid("[AEA PK-232M]")  # This MailDrop needs separate validation.
    msg = Message(msg_type=MSG_BULLETIN, to_call="ALL", at_bbs="WW",
                  from_call="A1AAA", bid="TEST123")
    assert _send_command(msg) == "SB ALL @ WW < A1AAA $TEST123"
    parsed = _parse_send(_send_command(msg), "B1BBB")
    assert (parsed.msg_type, parsed.to_call, parsed.at_bbs, parsed.from_call, parsed.bid) == (
        "B", "ALL", "WW", "A1AAA", "TEST123")
    assert _parse_send("SP A1AAA < B1BBB", "B1BBB").bid == ""
    assert _parse_send("SP A1AAA $" + "X" * 13, "B1BBB") is None
    msg.bid = "X" * 13
    with pytest.raises(ValueError, match="BID"):
        _send_command(msg)


def test_classic_routing_header_preserves_origin():
    cfg = AppConfig()
    cfg.node.node_call = "A1AAA-14"
    session = type("Session", (), {"_cfg": cfg})()
    msg = Message(id=3, body="R:261125/1200 9@C1CCC\nOriginal body")
    routed = _routing_body(session, msg)
    assert re.fullmatch(
        r"R:\d{6}/\d{4} 3@A1AAA\nR:261125/1200 9@C1CCC\n\nOriginal body",
        routed,
    )


@pytest.mark.asyncio
async def test_classic_outgoing_reverse_and_duplicate(tmp_path):
    store = BBSStore(str(tmp_path / "classic.db"))
    cfg = AppConfig()
    cfg.node.node_call = "A1AAA"
    await store.insert_message(Message(
        msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="ALL", at_bbs="WW",
        bid="CLASSIC01", subject="Forward", body="Hello KPC",
    ))
    seen = []

    async def kpc(reader, writer):
        wire = _WireReader(reader)
        writer.write(b"[KPC9612-8.2-HM$]\r486720 BYTES AVAILABLE\rENTER COMMAND: B,J,K,L,R,S, or Help >\r")
        await writer.drain()
        seen.append(await _line(wire))  # our H$ SID
        writer.write(b">\r")
        await writer.drain()
        proposal = await _line(wire)
        seen.append(proposal)
        if proposal.startswith("SB "):
            writer.write(b"O accepted\r")
            await writer.drain()
            seen.append(await _body(wire))
            writer.write(b">\r")
            await writer.drain()
            assert await _line(wire) == "F>"
        else:
            assert proposal == "F>"
        writer.write(b"SP A1AAA @ A1AAA < B1BBB $RETURN01\r")
        await writer.drain()
        answer = await _line(wire)
        seen.append(answer)
        if answer == "OK":
            writer.write(b"Return\rR:261126/0122 2@B1BBB.PA.USA.NOAM\rHello pyBulletin\r\x1a\r")
            await writer.drain()
        assert await _line(wire) == "F>"
        writer.write(b"*** DONE\r>\r")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(kpc, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(call="B1BBB", address=f"127.0.0.1:{port}", protocol="w0rli")
    cfg.forward.neighbors = [neighbor]
    try:
        assert await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5) == (1, 1)
        assert seen[1] == "SB ALL @ WW < A1AAA $CLASSIC01"
        assert seen[0].endswith("-H$]")
        assert seen[2][0] == "Forward"
        assert re.fullmatch(r"R:\d{6}/\d{4} 1@A1AAA\n\nHello KPC", seen[2][1])
        assert [(m.bid, m.body) for m in await store.list_messages() if m.bid == "RETURN01"] == [
            ("RETURN01", "R:261126/0122 2@B1BBB.PA.USA.NOAM\nHello pyBulletin")]
        assert await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5) == (0, 0)
        assert seen[-1] == "NO"
        assert len(await store.list_messages()) == 2
    finally:
        server.close()
        await server.wait_closed()
        await store.close()


@pytest.mark.asyncio
async def test_classic_disconnect_before_ack_keeps_message_pending(tmp_path):
    store = BBSStore(str(tmp_path / "retry.db"))
    cfg = AppConfig()
    cfg.node.node_call = "A1AAA"
    msg = Message(msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="ALL",
                  at_bbs="WW", bid="RETRY01", subject="Retry", body="Body")
    await store.insert_message(msg)

    async def interrupted_kpc(reader, writer):
        wire = _WireReader(reader)
        writer.write(b"[KPC9612-8.2-HM$]\r>\r")
        await writer.drain()
        await _line(wire)  # our SID
        writer.write(b">\r")
        await writer.drain()
        assert (await _line(wire)).startswith("SB ")
        writer.write(b"OK\r")
        await writer.drain()
        await _body(wire)
        writer.close()  # No final prompt: the sender cannot claim delivery.

    server = await asyncio.start_server(interrupted_kpc, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(call="B1BBB", address=f"127.0.0.1:{port}", protocol="w0rli")
    cfg.forward.neighbors = [neighbor]
    try:
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5)
        assert msg.id not in await store.delivered_message_ids("B1BBB")
    finally:
        server.close()
        await server.wait_closed()
        await store.close()


@pytest.mark.asyncio
async def test_classic_incoming_and_reverse(tmp_path):
    store = BBSStore(str(tmp_path / "incoming.db"))
    cfg = AppConfig()
    cfg.node.node_call = "A1AAA"
    neighbor = ForwardNeighborConfig(call="B1BBB", protocol="w0rli")
    cfg.forward.neighbors = [neighbor]
    await store.insert_message(Message(
        msg_type=MSG_PRIVATE, from_call="A1AAA", to_call="B1BBB", at_bbs="B1BBB",
        bid="OUT01", subject="For KPC", body="Reply text",
    ))

    async def incoming(reader, writer):
        await ForwardSession(cfg, store, neighbor).run_incoming(reader, writer)

    server = await asyncio.start_server(incoming, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        wire = _WireReader(reader)
        assert "-H$]" in await _line(wire)
        assert await _line(wire) == ">"
        writer.write(b"[KPC9612-8.2-HM$]\r")
        await writer.drain()
        assert await _line(wire) == ">"
        writer.write(b"SP A1AAA @ A1AAA < B1BBB $IN01\r")
        await writer.drain()
        assert await _line(wire) == "OK"
        writer.write(b"From KPC\rInbound body\r/EX\r")
        await writer.drain()
        assert await _line(wire) == ">"
        writer.write(b"F>\r")
        await writer.drain()
        assert await _line(wire) == "SP B1BBB @ B1BBB < A1AAA $OUT01"
        writer.write(b"O\r")
        await writer.drain()
        subject, body = await _body(wire)
        assert subject == "For KPC"
        assert re.fullmatch(r"R:\d{6}/\d{4} 1@A1AAA\n\nReply text", body)
        writer.write(b"F>\r")
        await writer.drain()
        assert await _line(wire) == "*** DONE"
        assert await _line(wire) == ">"
        writer.close()
        await writer.wait_closed()
        assert [(m.bid, m.body) for m in await store.list_messages() if m.bid == "IN01"] == [
            ("IN01", "Inbound body")]
        assert 1 in await store.delivered_message_ids("B1BBB")
    finally:
        server.close()
        await server.wait_closed()
        await store.close()
