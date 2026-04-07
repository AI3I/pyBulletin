"""Tests for BBSStore — message CRUD, filters, users, WP, search."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from pybulletin.store.store import BBSStore
from pybulletin.store.models import (
    Message, WPEntry,
    MSG_PRIVATE, MSG_BULLETIN, MSG_NTS,
    STATUS_NEW, STATUS_READ, STATUS_KILLED, STATUS_HELD, STATUS_FORWARDED,
    PRIV_SYSOP, PRIV_USER,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(to="W1AW", from_="W3BBS", subject="Test", body="Hello",
         msg_type=MSG_PRIVATE, status=STATUS_NEW) -> Message:
    return Message(
        msg_type=msg_type,
        from_call=from_,
        to_call=to,
        subject=subject,
        body=body,
        status=status,
    )


# ---------------------------------------------------------------------------
# Message insert / get
# ---------------------------------------------------------------------------

async def test_insert_and_get(store: BBSStore):
    msg = _msg()
    msg_id = await store.insert_message(msg)
    assert msg_id > 0

    fetched = await store.get_message(msg_id)
    assert fetched is not None
    assert fetched.to_call == "W1AW"
    assert fetched.subject == "Test"
    assert fetched.body == "Hello"
    assert fetched.size > 0


async def test_get_nonexistent(store: BBSStore):
    assert await store.get_message(99999) is None


async def test_bid_auto_generated(store: BBSStore):
    msg = _msg()
    msg_id = await store.insert_message(msg)
    fetched = await store.get_message(msg_id)
    assert fetched.bid != ""


async def test_size_computed(store: BBSStore):
    msg = _msg(body="Hello world")
    msg_id = await store.insert_message(msg)
    fetched = await store.get_message(msg_id)
    assert fetched.size == len("Hello world".encode())


# ---------------------------------------------------------------------------
# list_messages filters
# ---------------------------------------------------------------------------

async def test_list_excludes_killed_by_default(store: BBSStore):
    live_id = await store.insert_message(_msg(subject="Live"))
    dead_id = await store.insert_message(_msg(subject="Dead"))
    await store.kill_message(dead_id)

    msgs = await store.list_messages()
    ids = [m.id for m in msgs]
    assert live_id in ids
    assert dead_id not in ids


async def test_list_killed_explicitly(store: BBSStore):
    dead_id = await store.insert_message(_msg(subject="Dead"))
    await store.kill_message(dead_id)

    msgs = await store.list_messages(status=STATUS_KILLED)
    assert any(m.id == dead_id for m in msgs)


async def test_list_filter_to_call(store: BBSStore):
    id1 = await store.insert_message(_msg(to="W1AW"))
    id2 = await store.insert_message(_msg(to="K9ZZZ"))

    mine = await store.list_messages(to_call="W1AW")
    ids = [m.id for m in mine]
    assert id1 in ids
    assert id2 not in ids


async def test_list_filter_msg_type(store: BBSStore):
    priv_id = await store.insert_message(_msg(msg_type=MSG_PRIVATE))
    bull_id = await store.insert_message(_msg(msg_type=MSG_BULLETIN))

    bulls = await store.list_messages(msg_type=MSG_BULLETIN)
    ids = [m.id for m in bulls]
    assert bull_id in ids
    assert priv_id not in ids


async def test_list_filter_since_id(store: BBSStore):
    id1 = await store.insert_message(_msg(subject="Old"))
    id2 = await store.insert_message(_msg(subject="New"))

    msgs = await store.list_messages(since_id=id1)
    ids = [m.id for m in msgs]
    assert id1 not in ids
    assert id2 in ids


async def test_list_filter_after_date(store: BBSStore):
    old_msg = _msg(subject="Old")
    old_msg.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await store.insert_message(old_msg)
    new_id = await store.insert_message(_msg(subject="New"))

    cutoff = datetime(2023, 1, 1, tzinfo=timezone.utc)
    msgs = await store.list_messages(after_date=cutoff)
    ids = [m.id for m in msgs]
    assert new_id in ids
    assert all(m.subject != "Old" for m in msgs)


async def test_list_search_subject(store: BBSStore):
    id1 = await store.insert_message(_msg(subject="Wanted"))
    id2 = await store.insert_message(_msg(subject="Ignored"))

    results = await store.list_messages(search="Wanted")
    ids = [m.id for m in results]
    assert id1 in ids
    assert id2 not in ids


async def test_list_limit_and_reverse(store: BBSStore):
    for i in range(5):
        await store.insert_message(_msg(subject=f"Msg{i}"))

    last2 = await store.list_messages(limit=2, reverse=True)
    assert len(last2) == 2
    # reverse=True returns DESC, so highest id first
    assert last2[0].id > last2[1].id


# ---------------------------------------------------------------------------
# count_messages
# ---------------------------------------------------------------------------

async def test_count_excludes_killed(store: BBSStore):
    await store.insert_message(_msg())
    dead_id = await store.insert_message(_msg())
    await store.kill_message(dead_id)

    total = await store.count_messages()
    assert total == 1


async def test_count_by_type_and_status(store: BBSStore):
    await store.insert_message(_msg(msg_type=MSG_BULLETIN, status=STATUS_NEW))
    await store.insert_message(_msg(msg_type=MSG_PRIVATE,  status=STATUS_NEW))

    n = await store.count_messages(msg_type=MSG_BULLETIN, status=STATUS_NEW)
    assert n == 1


# ---------------------------------------------------------------------------
# Message status transitions
# ---------------------------------------------------------------------------

async def test_mark_read(store: BBSStore):
    msg_id = await store.insert_message(_msg())
    ok = await store.mark_read(msg_id, "W1AW")
    assert ok
    fetched = await store.get_message(msg_id)
    assert fetched.status == STATUS_READ
    assert fetched.read_by == "W1AW"


async def test_kill_message(store: BBSStore):
    msg_id = await store.insert_message(_msg())
    assert await store.kill_message(msg_id)
    fetched = await store.get_message(msg_id)
    assert fetched.status == STATUS_KILLED


async def test_hold_and_release(store: BBSStore):
    msg_id = await store.insert_message(_msg())
    assert await store.hold_message(msg_id)
    assert (await store.get_message(msg_id)).status == STATUS_HELD

    assert await store.release_message(msg_id)
    assert (await store.get_message(msg_id)).status == STATUS_NEW


async def test_mark_forwarded(store: BBSStore):
    msg_id = await store.insert_message(_msg())
    assert await store.mark_forwarded(msg_id)
    assert (await store.get_message(msg_id)).status == STATUS_FORWARDED


# ---------------------------------------------------------------------------
# Message update (sysop edit)
# ---------------------------------------------------------------------------

async def test_update_message(store: BBSStore):
    msg_id = await store.insert_message(_msg(subject="Old", body="Old body"))
    now = datetime.now(timezone.utc)
    ok = await store.update_message(
        msg_id, subject="New", body="New body",
        edited_by="AI3I", edited_at=now,
    )
    assert ok
    fetched = await store.get_message(msg_id)
    assert fetched.subject == "New"
    assert fetched.body == "New body"
    assert fetched.edited_by == "AI3I"


# ---------------------------------------------------------------------------
# BID duplicate detection
# ---------------------------------------------------------------------------

async def test_has_bid(store: BBSStore):
    msg = _msg()
    msg.bid = "W3BBS240101120000"
    await store.insert_message(msg)
    assert await store.has_bid("W3BBS240101120000")
    assert not await store.has_bid("NONEXISTENT999")


# ---------------------------------------------------------------------------
# Forward path
# ---------------------------------------------------------------------------

async def test_append_forward_path(store: BBSStore):
    msg_id = await store.insert_message(_msg())
    await store.append_forward_path(msg_id, "W3BBS")
    await store.append_forward_path(msg_id, "K9ZZZ")
    fetched = await store.get_message(msg_id)
    assert "W3BBS" in fetched.forward_path
    assert "K9ZZZ" in fetched.forward_path


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def test_record_login_creates_user(store: BBSStore):
    user = await store.record_login("W1NEW", "10.0.0.1")
    assert user.call == "W1NEW"
    assert user.last_login_peer == "10.0.0.1"
    assert user.last_login_at is not None


async def test_record_login_idempotent(store: BBSStore):
    u1 = await store.record_login("W1DUP", "1.2.3.4")
    u2 = await store.record_login("W1DUP", "5.6.7.8")
    assert u2.last_login_peer == "5.6.7.8"
    users = await store.list_users()
    assert sum(1 for u in users if u.call == "W1DUP") == 1


async def test_upsert_and_get_user(store: BBSStore):
    user = await store.record_login("W1UPS", "x")
    user.privilege = PRIV_SYSOP
    user.display_name = "Test Sysop"
    await store.upsert_user(user)

    fetched = await store.get_user("W1UPS")
    assert fetched.privilege == PRIV_SYSOP
    assert fetched.display_name == "Test Sysop"


async def test_set_privilege(store: BBSStore):
    await store.record_login("W1PRV", "x")
    ok = await store.set_privilege("W1PRV", PRIV_SYSOP)
    assert ok
    user = await store.get_user("W1PRV")
    assert user.privilege == PRIV_SYSOP


async def test_delete_user(store: BBSStore):
    await store.record_login("W1DEL", "x")
    assert await store.delete_user("W1DEL")
    assert await store.get_user("W1DEL") is None


async def test_list_users_search(store: BBSStore):
    await store.record_login("W1AAA", "x")
    await store.record_login("W2BBB", "x")
    results = await store.list_users(search="W1")
    assert all("W1" in u.call for u in results)


# ---------------------------------------------------------------------------
# White Pages
# ---------------------------------------------------------------------------

async def test_upsert_and_get_wp(store: BBSStore):
    entry = WPEntry(call="W1WP", home_bbs="W3BBS", name="Test User")
    await store.upsert_wp_entry(entry)
    fetched = await store.get_wp_entry("W1WP")
    assert fetched is not None
    assert fetched.home_bbs == "W3BBS"
    assert fetched.name == "Test User"


async def test_wp_upsert_updates(store: BBSStore):
    await store.upsert_wp_entry(WPEntry(call="W1UP", home_bbs="OLD"))
    await store.upsert_wp_entry(WPEntry(call="W1UP", home_bbs="NEW"))
    fetched = await store.get_wp_entry("W1UP")
    assert fetched.home_bbs == "NEW"


async def test_search_wp_by_call(store: BBSStore):
    await store.upsert_wp_entry(WPEntry(call="W1FIND", name="Alice"))
    await store.upsert_wp_entry(WPEntry(call="K9OTHER", name="Bob"))
    results = await store.search_wp("W1FIND")
    assert any(e.call == "W1FIND" for e in results)
    assert all(e.call != "K9OTHER" for e in results)


async def test_search_wp_by_name(store: BBSStore):
    await store.upsert_wp_entry(WPEntry(call="W1NAME", name="Findable"))
    await store.upsert_wp_entry(WPEntry(call="K9OTHER", name="Other"))
    results = await store.search_wp("Findable")
    assert any(e.call == "W1NAME" for e in results)


async def test_count_wp_entries(store: BBSStore):
    await store.upsert_wp_entry(WPEntry(call="W1C1"))
    await store.upsert_wp_entry(WPEntry(call="W1C2"))
    assert await store.count_wp_entries() == 2


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------

async def test_set_get_user_pref(store: BBSStore):
    await store.record_login("W1PREF", "x")
    await store.set_user_pref("W1PREF", "cols", "80")
    val = await store.get_user_pref("W1PREF", "cols")
    assert val == "80"


async def test_delete_user_pref(store: BBSStore):
    await store.record_login("W1DPREF", "x")
    await store.set_user_pref("W1DPREF", "k", "v")
    assert await store.delete_user_pref("W1DPREF", "k")
    assert await store.get_user_pref("W1DPREF", "k") is None


# ---------------------------------------------------------------------------
# Retention / cleanup
# ---------------------------------------------------------------------------

async def test_cleanup_killed(store: BBSStore):
    msg_id = await store.insert_message(_msg())
    await store.kill_message(msg_id)

    # Force created_at to be old so retention picks it up
    store._conn.execute(
        "UPDATE messages SET created_at = 0 WHERE id = ?", (msg_id,)
    )
    store._conn.commit()

    removed = await store.cleanup_expired(
        personal_days=9999, bulletin_days=9999, nts_days=9999, killed_days=0
    )
    assert removed >= 1
    assert await store.get_message(msg_id) is None
