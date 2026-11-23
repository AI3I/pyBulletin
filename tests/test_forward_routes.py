"""Forwarding route decisions shared by the sender and pending counts."""
from __future__ import annotations

import pytest

from pybulletin.config import ForwardNeighborConfig
from pybulletin.forward.routes import matching_neighbors
from pybulletin.store.models import Message, MSG_BULLETIN, MSG_NTS, MSG_PRIVATE
from pybulletin.store.store import BBSStore


def _calls(message, neighbors):
    return [n.call for n in matching_neighbors(message, neighbors, node_call="MYBBS")]


def test_area_bulletin_uses_at_address_and_floods_matching_peers():
    neighbors = [
        ForwardNeighborConfig(call="W1AAA", categories=["WW"]),
        ForwardNeighborConfig(call="W2BBB", categories=["WW"]),
        ForwardNeighborConfig(call="W3CCC", categories=["USA.NA"]),
    ]
    msg = Message(msg_type=MSG_BULLETIN, to_call="ALL", at_bbs="WW")
    assert _calls(msg, neighbors) == ["W1AAA", "W2BBB"]
    msg.at_bbs = "USA.NA"
    assert _calls(msg, neighbors) == ["W1AAA", "W2BBB", "W3CCC"]
    msg.forward_path = "W1AAA"
    assert _calls(msg, neighbors) == ["W2BBB", "W3CCC"]


def test_directed_mail_chooses_one_most_specific_route():
    neighbors = [
        ForwardNeighborConfig(call="W1AAA", categories=["WW"]),
        ForwardNeighborConfig(call="W2BBB", categories=["USA.NA"]),
        ForwardNeighborConfig(call="W3CCC", categories=["CA.USA.NA"]),
    ]
    msg = Message(msg_type=MSG_PRIVATE, to_call="N1USER", at_bbs="X1BBS.CA.USA.NA")
    assert _calls(msg, neighbors) == ["W3CCC"]
    msg.forward_path = "W3CCC"
    assert _calls(msg, neighbors) == ["W2BBB"]
    msg.forward_path = "MYBBS"
    assert _calls(msg, neighbors) == []


def test_direct_bbs_route_wins_and_directed_bulletin_is_single_route():
    neighbors = [
        ForwardNeighborConfig(call="W1AAA", categories=["USA.NA"]),
        ForwardNeighborConfig(call="X1BBS", categories=["WW"]),
    ]
    msg = Message(msg_type=MSG_PRIVATE, to_call="N1USER", at_bbs="X1BBS.CA.USA.NA")
    assert _calls(msg, neighbors) == ["X1BBS"]
    msg.msg_type = MSG_BULLETIN
    msg.to_call = "ALL"
    assert _calls(msg, neighbors) == ["X1BBS"]


def test_nts_is_single_route_and_area_match_has_component_boundary():
    neighbors = [
        ForwardNeighborConfig(call="W1AAA", categories=["US"]),
        ForwardNeighborConfig(call="W2BBB", categories=["USA.NA"]),
        ForwardNeighborConfig(call="W3CCC", categories=["USA.NA"]),
    ]
    msg = Message(msg_type=MSG_NTS, to_call="USA.NA")
    assert _calls(msg, neighbors) == ["W2BBB"]


@pytest.mark.asyncio
async def test_pending_counts_use_same_routes(tmp_path):
    store = BBSStore(str(tmp_path / "routes.db"))
    neighbors = [
        ForwardNeighborConfig(call="W1AAA", categories=["WW"]),
        ForwardNeighborConfig(call="W2BBB", categories=["USA.NA"]),
        ForwardNeighborConfig(call="W3CCC", categories=["CA.USA.NA"]),
    ]
    try:
        await store.insert_message(Message(
            msg_type=MSG_PRIVATE, to_call="N1USER", at_bbs="X1BBS.CA.USA.NA",
            from_call="MYBBS", subject="Private", body="Hello",
        ))
        await store.insert_message(Message(
            msg_type=MSG_BULLETIN, to_call="ALL", at_bbs="CA.USA.NA",
            from_call="MYBBS", subject="Bulletin", body="World",
        ))
        counts = await store.pending_delivery_counts(neighbors, "MYBBS")
        assert counts == {"W1AAA": 1, "W2BBB": 1, "W3CCC": 2}
    finally:
        await store.close()
