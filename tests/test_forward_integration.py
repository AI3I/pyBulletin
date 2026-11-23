"""Exercise forwarding with two real TCP streams and independent stores."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from pybulletin.config import AppConfig, ForwardNeighborConfig, load_config, save_config
from pybulletin.ax25.router import AX25Router
from pybulletin.forward.session import ForwardSession, inbound_neighbor, _WireReader
from pybulletin.forward.binary import encode_fa, decode_fa
from pybulletin.forward.protocol import lzhuf_compress, lzhuf_decompress
from pybulletin.store.models import Message, MSG_BULLETIN, MSG_PRIVATE
from pybulletin.store.store import BBSStore


def test_lzhuf_matches_linbpq_reference_vector():
    original = b"Hello from pyBulletin\r\n"
    reference = bytes.fromhex(
        "17000000ea7c7f187fbd67cbfd85f9c1fe02f3806ddcc400fafeb33960"
    )
    assert lzhuf_compress(original) == reference
    assert lzhuf_decompress(reference) == original
    repeated_reference = bytes.fromhex("5a000000e6f3b9fcd0152008")
    assert lzhuf_decompress(repeated_reference) == b"ABC" * 30


async def test_fbb_b0_frame_roundtrip_and_checksum():
    frame = encode_fa("Subject", "Hello from pyBulletin")
    reader = asyncio.StreamReader()
    reader.feed_data(frame)
    reader.feed_eof()
    assert await decode_fa(reader) == ("Subject", "Hello from pyBulletin")

    damaged = frame[:-1] + bytes([frame[-1] ^ 1])
    reader = asyncio.StreamReader()
    reader.feed_data(damaged)
    reader.feed_eof()
    with pytest.raises(ValueError, match="checksum"):
        await decode_fa(reader)

    long_body = "\n".join(f"{i:04x} {i*i:08x} LinBPQ and FBB" for i in range(100))
    reader = asyncio.StreamReader()
    reader.feed_data(encode_fa("Multiblock", long_body))
    reader.feed_eof()
    assert await decode_fa(reader) == ("Multiblock", long_body)


def test_bpq_tcp_credentials_config_roundtrip(tmp_path):
    base = tmp_path / "pybulletin.toml"
    base.write_text("", encoding="utf-8")
    cfg = AppConfig()
    cfg.forward.neighbors = [ForwardNeighborConfig(
        call="N0CALL", address="127.0.0.1:18011",
        login_user="lab", login_password='pa\\ss"word',
    )]
    save_config(cfg, str(base))
    assert (tmp_path / "pybulletin.local.toml").stat().st_mode & 0o077 == 0
    loaded = load_config(str(base))
    assert loaded.forward.neighbors[0].login_user == "lab"
    assert loaded.forward.neighbors[0].login_password == 'pa\\ss"word'


async def test_inbound_peer_policy_and_delivery_status(tmp_path):
    store = BBSStore(str(tmp_path / "status.db"))
    cfg = AppConfig()
    cfg.forward.neighbors = [ForwardNeighborConfig(call="B1BBB", categories=["WW"])]
    assert inbound_neighbor(cfg.forward, "X1XXX") is None
    assert inbound_neighbor(cfg.forward, "B1BBB") is cfg.forward.neighbors[0]
    cfg.forward.neighbors[0].inbound_host = "192.0.2.10"
    assert inbound_neighbor(cfg.forward, "BPQ", "192.0.2.10") is cfg.forward.neighbors[0]
    cfg.forward.neighbors[0].enabled = False
    assert inbound_neighbor(cfg.forward, "B1BBB") is None
    cfg.forward.allow_unknown_peers = True
    assert inbound_neighbor(cfg.forward, "X1XXX") is not None
    assert inbound_neighbor(cfg.forward, "B1BBB") is None

    msg = Message(msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW", body="Hello")
    await store.insert_message(msg)
    assert (await store.pending_delivery_counts(cfg.forward.neighbors))["B1BBB"] == 1
    await store.mark_delivered(msg.id, "B1BBB")
    assert (await store.pending_delivery_counts(cfg.forward.neighbors))["B1BBB"] == 0
    await store.record_forward_result("B1BBB", error="link down")
    assert (await store.forward_errors())["B1BBB"] == "link down"
    await store.record_forward_result("B1BBB", sent=1)
    assert (await store.forward_errors())["B1BBB"] == ""
    await store.close()


async def test_forward_schema_upgrade_and_hot_backup_restore(tmp_path):
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (3)")
        conn.execute("CREATE TABLE sequence (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO sequence VALUES ('msg_id', 0)")
        conn.execute("""CREATE TABLE forward_neighbors (
            call TEXT PRIMARY KEY, last_connect_at INTEGER, last_success_at INTEGER,
            msgs_sent INTEGER NOT NULL DEFAULT 0, msgs_received INTEGER NOT NULL DEFAULT 0,
            session_active INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1
        )""")
    store = BBSStore(str(db_path))
    msg = Message(msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW", body="Saved")
    await store.insert_message(msg)
    await store.mark_delivered(msg.id, "B1BBB")
    await store.record_forward_result("B1BBB", sent=1)
    backup = tmp_path / "backup.db"
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    restored = BBSStore(str(backup))
    try:
        assert (await restored.get_message(msg.id)).body == "Saved"
        assert msg.id in await restored.delivered_message_ids("B1BBB")
        assert (await restored.list_neighbors())[0].msgs_sent == 1
    finally:
        await restored.close()
        await store.close()


async def test_interrupted_transfer_stays_pending(tmp_path):
    store = BBSStore(str(tmp_path / "retry.db"))
    cfg = AppConfig()
    cfg.node.node_call = "A1AAA"
    msg = Message(msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW", body="Retry me")
    await store.insert_message(msg)

    async def interrupted(reader, writer):
        wire = _WireReader(reader)
        writer.write(b"[B1BBB-pyBulletin1.0-FHM$]\r\n>\r\n")
        await writer.drain()
        await wire.readline()
        while True:
            line = await wire.readline()
            if line.strip() == b"F>":
                break
            if not line:
                return
        writer.write(b"FS +\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(interrupted, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(call="B1BBB", address=f"127.0.0.1:{port}")
    try:
        with pytest.raises((ConnectionError, OSError)):
            await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5)
        assert msg.id not in await store.delivered_message_ids("B1BBB")
        assert (await store.pending_delivery_counts([neighbor]))["B1BBB"] == 1
    finally:
        server.close()
        await server.wait_closed()
        await store.close()


async def test_fbb_ascii_wire_transcript(tmp_path):
    store = BBSStore(str(tmp_path / "wire.db"))
    cfg = AppConfig()
    cfg.node.node_call = "A1AAA"
    msg = Message(msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW", subject="News", body="Line one\nLine two")
    await store.insert_message(msg)
    transcript = []

    async def peer(reader, writer):
        wire = _WireReader(reader)
        writer.write(b"[BPQ-6.0-FHM$]\r>\r")
        await writer.drain()
        transcript.append((await wire.readline()).strip())
        transcript.append((await wire.readline()).strip())
        transcript.append((await wire.readline()).strip())
        writer.write(b"FS +\r")
        await writer.drain()
        for _ in range(4):
            transcript.append((await wire.readline()).strip())
        writer.write(b"FF\r")
        await writer.drain()
        transcript.append((await wire.readline()).strip())
        writer.write(b"FQ\r")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(call="B1BBB", address=f"127.0.0.1:{port}")
    try:
        assert (await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5))[0] == 1
        await asyncio.sleep(0)
        assert transcript[0].startswith(b"[A1AAA-pyBulletin")
        assert transcript[1] == f"FB B A1AAA WW WW {msg.bid} {msg.size}".encode()
        assert transcript[2] == b"F>"
        assert transcript[3:7] == [b"News", b"Line one", b"Line two", b"\x1a"]
        assert transcript[7] == b"FF"
        assert msg.id in await store.delivered_message_ids("B1BBB")
    finally:
        server.close()
        await server.wait_closed()
        await store.close()


async def test_bpq_fbbport_login_and_banner(tmp_path):
    store = BBSStore(str(tmp_path / "bpq-login.db"))
    cfg = AppConfig()
    cfg.node.node_call = "N1CALL"
    transcript = []

    async def bpq_peer(reader, writer):
        wire = _WireReader(reader)
        transcript.append((await wire.readline()).strip())
        transcript.append((await wire.readline()).strip())
        writer.write(b"BPQLAB:N0CALL-7} Connected to BBS\r")
        writer.write(b"[BPQ-6.0.26.1-B2FWIHJM$]\r>\r")
        await writer.drain()
        transcript.append((await wire.readline()).strip())
        transcript.append((await wire.readline()).strip())
        writer.write(b"FQ\r")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(bpq_peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(
        call="N0CALL", address=f"127.0.0.1:{port}",
        login_user="lab", login_password="labpass",
    )
    try:
        assert await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5) == (0, 0)
        assert transcript[:2] == [b"lab", b"labpass"]
        assert transcript[2].startswith(b"[N1CALL-pyBulletin")
        assert transcript[3] == b"FF"
    finally:
        server.close()
        await server.wait_closed()
        await store.close()


async def test_bpq_b0_compressed_forwarding(tmp_path):
    store = BBSStore(str(tmp_path / "bpq-b0.db"))
    cfg = AppConfig()
    cfg.node.node_call = "N1CALL"
    msg = Message(msg_type=MSG_BULLETIN, from_call="N1CALL", to_call="WW",
                  at_bbs="WW", subject="BPQ live test", body="Hello from pyBulletin")
    await store.insert_message(msg)
    seen = {}

    async def bpq_peer(reader, writer):
        wire = _WireReader(reader)
        assert (await wire.readline()).strip() == b"lab"
        assert (await wire.readline()).strip() == b"labpass"
        writer.write(b"BPQLAB:N0CALL-7} Connected to BBS\r")
        writer.write(b"[BPQ-6.0.26.1-B1FWIHJM$]\rde N0CALL>\r")
        await writer.drain()
        seen["sid"] = (await wire.readline()).strip()
        seen["proposal"] = (await wire.readline()).strip()
        end = (await wire.readline()).strip()
        assert end.startswith(b"F> ")
        assert (sum(seen["proposal"] + b"\r") + int(end[3:], 16)) & 0xff == 0
        writer.write(b"FS +\r")
        await writer.drain()
        seen["message"] = await decode_fa(wire)
        writer.write(b"FF\r")
        await writer.drain()
        assert (await wire.readline()).strip() == b"FF"
        writer.write(b"FQ\r")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(bpq_peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(call="N0CALL", address=f"127.0.0.1:{port}",
                                     login_user="lab", login_password="labpass", protocol="b0")
    try:
        assert await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5) == (1, 0)
        assert b"-BFH$]" in seen["sid"]
        assert seen["proposal"].startswith(b"FA B N1CALL WW WW ")
        assert seen["message"] == ("BPQ live test", "Hello from pyBulletin")
        assert msg.id in await store.delivered_message_ids("N0CALL")
    finally:
        server.close()
        await server.wait_closed()
        await store.close()


async def test_bpq_b0_reverse_forwarding_and_proposal_checksum(tmp_path):
    store = BBSStore(str(tmp_path / "bpq-reverse.db"))
    cfg = AppConfig()
    cfg.node.node_call = "N1CALL"

    async def bpq_peer(reader, writer):
        wire = _WireReader(reader)
        await wire.readline()  # login user
        await wire.readline()  # password
        writer.write(b"[BPQ-6.0.26.1-B1FWIHJM$]\rde N0CALL>\r")
        await writer.drain()
        assert b"-BFH$]" in (await wire.readline())
        assert (await wire.readline()).strip() == b"FF"
        proposal = b"FA P N0CALL N1CALL N1CALL BPQTEST01 18\r"
        checksum = -sum(proposal) & 0xff
        writer.write(proposal + f"F> {checksum:02X}\r".encode())
        await writer.drain()
        assert (await wire.readline()).strip() == b"FS +"
        writer.write(encode_fa("Reverse", "Hello from LinBPQ"))
        await writer.drain()
        assert (await wire.readline()).strip() == b"FF"
        writer.write(b"FQ\r")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(bpq_peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(call="N0CALL", address=f"127.0.0.1:{port}",
                                     login_user="lab", login_password="labpass", protocol="b0")
    try:
        assert await asyncio.wait_for(ForwardSession(cfg, store, neighbor).run_outgoing(), 5) == (0, 1)
        messages = await store.list_messages()
        assert len(messages) == 1
        assert (messages[0].bid, messages[0].subject, messages[0].body) == (
            "BPQTEST01", "Reverse", "Hello from LinBPQ")
    finally:
        server.close()
        await server.wait_closed()
        await store.close()


async def test_ax25_forwarding_between_two_routers(tmp_path):
    a = BBSStore(str(tmp_path / "radio-a.db"))
    b = BBSStore(str(tmp_path / "radio-b.db"))
    cfg_a, cfg_b = AppConfig(), AppConfig()
    cfg_a.node.node_call = "A1AAA"
    cfg_b.node.node_call = "B1BBB"
    cfg_a.forward.neighbors = [ForwardNeighborConfig(call="B1BBB", address="B1BBB")]
    cfg_b.forward.neighbors = [ForwardNeighborConfig(call="A1AAA")]
    msg = Message(msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW", subject="RF", body="Radio body")
    await a.insert_message(msg)
    router_a = router_b = None

    async def send_a(frame, port):
        await router_b.handle_frame(frame, port)

    async def send_b(frame, port):
        await router_a.handle_frame(frame, port)

    router_a = AX25Router(cfg_a, a, None, send_a)
    router_b = AX25Router(cfg_b, b, None, send_b)
    try:
        result = await asyncio.wait_for(
            ForwardSession(cfg_a, a, cfg_a.forward.neighbors[0], router_a).run_outgoing(), 5)
        assert result[0] == 1
        assert (await b.list_messages())[0].body == "Radio body"
        assert msg.id in await a.delivered_message_ids("B1BBB")
        if router_b._session_tasks:
            await asyncio.wait_for(asyncio.gather(*router_b._session_tasks.values()), 2)
    finally:
        await a.close()
        await b.close()


async def test_fbb_batch_limit_and_oversize_defer(tmp_path):
    store = BBSStore(str(tmp_path / "limits.db"))
    cfg = AppConfig()
    cfg.node.node_call = "A1AAA"
    neighbor = ForwardNeighborConfig(call="B1BBB", categories=["WW"])
    for index in range(6):
        await store.insert_message(Message(
            msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW",
            subject=f"Bulletin {index}", body="hello",
        ))
    session = ForwardSession(cfg, store, neighbor)
    try:
        assert len(await session._select_outgoing()) == 5
        from pybulletin.forward.protocol import Proposal
        output = []

        async def capture(line):
            output.append(line)

        session._writeline = capture
        await session._respond_to_proposals([Proposal(
            to_call="WW", from_call="B1BBB", bid="OVERSIZE", size=256001,
        )])
        assert output == ["FS ="]
    finally:
        await store.close()


async def test_multiple_fbb_batches_drain_in_one_connection(tmp_path):
    a = BBSStore(str(tmp_path / "multi-a.db"))
    b = BBSStore(str(tmp_path / "multi-b.db"))
    cfg_a, cfg_b = AppConfig(), AppConfig()
    cfg_a.node.node_call = "A1AAA"
    cfg_b.node.node_call = "B1BBB"
    for index in range(6):
        await a.insert_message(Message(
            msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW",
            subject=f"Batch {index}", body=f"Body {index}",
        ))
    await b.insert_message(Message(
        msg_type=MSG_PRIVATE, from_call="B1BBB", to_call="A1AAA",
        at_bbs="A1AAA", subject="Reply", body="Back to A",
    ))

    async def incoming(reader, writer):
        await ForwardSession(cfg_b, b, ForwardNeighborConfig(call="A1AAA")).run_incoming(reader, writer)

    server = await asyncio.start_server(incoming, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    neighbor = ForwardNeighborConfig(call="B1BBB", address=f"127.0.0.1:{port}")
    try:
        sent, received = await asyncio.wait_for(ForwardSession(cfg_a, a, neighbor).run_outgoing(), 5)
        assert (sent, received) == (6, 1)
        assert len(await b.list_messages()) == 7
        assert len(await a.delivered_message_ids("B1BBB")) == 6
        assert any(m.subject == "Reply" for m in await a.list_messages())
    finally:
        server.close()
        await server.wait_closed()
        await a.close()
        await b.close()


async def test_bulletin_reaches_each_neighbor_and_is_acknowledged(tmp_path):
    a = BBSStore(str(tmp_path / "a.db"))
    b = BBSStore(str(tmp_path / "b.db"))
    c = BBSStore(str(tmp_path / "c.db"))
    cfg_a, cfg_b, cfg_c = AppConfig(), AppConfig(), AppConfig()
    cfg_a.node.node_call = "A1AAA"
    cfg_b.node.node_call = "B1BBB"
    cfg_c.node.node_call = "C1CCC"
    msg = Message(msg_type=MSG_BULLETIN, from_call="A1AAA", to_call="WW", subject="Test", body="Hello")
    await a.insert_message(msg)

    async def exchange(remote_cfg, remote_store):
        errors = []

        async def incoming(reader, writer):
            try:
                await ForwardSession(remote_cfg, remote_store, ForwardNeighborConfig(call="A1AAA")).run_incoming(reader, writer)
            except Exception as exc:
                errors.append(exc)

        server = await asyncio.start_server(incoming, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        neighbor = ForwardNeighborConfig(call=remote_cfg.node.node_call, address=f"127.0.0.1:{port}", categories=["WW"])
        try:
            result = await asyncio.wait_for(ForwardSession(cfg_a, a, neighbor).run_outgoing(), 5)
            await asyncio.sleep(0)
            assert not errors
            return neighbor, result
        finally:
            server.close()
            await server.wait_closed()

    try:
        n_b, first = await exchange(cfg_b, b)
        assert first[0] == 1
        assert len(await b.list_messages()) == 1
        assert msg.id in await a.delivered_message_ids(n_b.call)
        _, second = await exchange(cfg_b, b)
        assert second[0] == 0
        assert msg.id in await a.delivered_message_ids(n_b.call)
        n_c, third = await exchange(cfg_c, c)
        assert third[0] == 1
        assert len(await c.list_messages()) == 1
        assert msg.id in await a.delivered_message_ids(n_c.call)
    finally:
        await a.close()
        await b.close()
        await c.close()
