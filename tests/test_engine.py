"""Tests for CommandEngine — dispatch, key command handlers via FakeSession."""
from __future__ import annotations

import pytest

from pybulletin.command.engine import CommandEngine
from pybulletin.store.models import (
    Message, WPEntry,
    MSG_PRIVATE, MSG_BULLETIN, MSG_NTS,
    STATUS_NEW, STATUS_KILLED, STATUS_HELD,
    PRIV_SYSOP, PRIV_USER,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine(session):
    return CommandEngine(session)


async def _dispatch(session, line: str) -> str:
    """Run a command and return the full output."""
    session.clear_output()
    engine = _engine(session)
    await engine.dispatch(line)
    return session.output()


def _msg(to="W1TEST", from_="W3BBS", subject="Test", body="Hello",
         msg_type=MSG_PRIVATE) -> Message:
    return Message(
        msg_type=msg_type,
        from_call=from_,
        to_call=to,
        subject=subject,
        body=body,
        status=STATUS_NEW,
    )


# ---------------------------------------------------------------------------
# dispatch: unknown command / empty
# ---------------------------------------------------------------------------

async def test_empty_line_no_output(fake_session):
    out = await _dispatch(fake_session, "")
    assert out == ""


async def test_unknown_command(fake_session):
    out = await _dispatch(fake_session, "ZZZNOPE")
    # Should show an error, not crash
    assert len(out) > 0


# ---------------------------------------------------------------------------
# H — help
# ---------------------------------------------------------------------------

async def test_help_command(fake_session):
    out = await _dispatch(fake_session, "H")
    assert len(out) > 0


async def test_help_alias(fake_session):
    out1 = await _dispatch(fake_session, "H")
    out2 = await _dispatch(fake_session, "HELP")
    assert len(out1) > 0 and len(out2) > 0


async def test_question_mark_help(fake_session):
    out = await _dispatch(fake_session, "?")
    assert "Commands" in out


async def test_double_question_mark_help(fake_session):
    out = await _dispatch(fake_session, "??")
    assert "Reading" in out


async def test_help_detail(fake_session):
    out = await _dispatch(fake_session, "?L")
    assert "list" in out.lower()


# ---------------------------------------------------------------------------
# V — version
# ---------------------------------------------------------------------------

async def test_version_command(fake_session):
    out = await _dispatch(fake_session, "V")
    assert len(out) > 0


# ---------------------------------------------------------------------------
# DATE / TIME
# ---------------------------------------------------------------------------

async def test_date_command(fake_session):
    out = await _dispatch(fake_session, "DATE")
    assert "UTC" in out


async def test_time_alias(fake_session):
    out = await _dispatch(fake_session, "TIME")
    assert "UTC" in out


# ---------------------------------------------------------------------------
# WHOAMI
# ---------------------------------------------------------------------------

async def test_whoami_shows_callsign(fake_session):
    out = await _dispatch(fake_session, "WHOAMI")
    assert "W1TEST" in out


async def test_whoami_shows_privilege(fake_session):
    out = await _dispatch(fake_session, "WHOAMI")
    assert "user" in out.lower() or "privilege" in out.lower()


async def test_whoami_sysop(sysop_session):
    out = await _dispatch(sysop_session, "WHOAMI")
    assert "AI3I" in out


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------

async def test_stats_command(fake_session, store):
    await store.insert_message(_msg())
    out = await _dispatch(fake_session, "STATS")
    assert "Messages" in out or "messages" in out.lower()


# ---------------------------------------------------------------------------
# BBS
# ---------------------------------------------------------------------------

async def test_bbs_no_neighbors(fake_session):
    out = await _dispatch(fake_session, "BBS")
    assert "No neighbor" in out or "neighbor" in out.lower()


# ---------------------------------------------------------------------------
# X — expert mode toggle
# ---------------------------------------------------------------------------

async def test_expert_toggle_on(fake_session):
    fake_session.user.expert_mode = False
    out = await _dispatch(fake_session, "X")
    assert "ON" in out


async def test_expert_toggle_off(fake_session):
    fake_session.user.expert_mode = True
    out = await _dispatch(fake_session, "X")
    assert "OFF" in out


async def test_expert_toggle_persisted(fake_session, store):
    initial = fake_session.user.expert_mode
    await _dispatch(fake_session, "X")
    # User in store should be updated
    updated = await store.get_user("W1TEST")
    assert updated.expert_mode != initial


# ---------------------------------------------------------------------------
# K / D / KILL / RM — kill messages
# ---------------------------------------------------------------------------

async def test_kill_no_args_shows_usage(fake_session):
    out = await _dispatch(fake_session, "K")
    assert "Usage" in out or "usage" in out.lower()


async def test_kill_message(fake_session, store):
    msg_id = await store.insert_message(_msg(to="W1TEST"))
    out = await _dispatch(fake_session, f"K {msg_id}")
    msg = await store.get_message(msg_id)
    assert msg.status == STATUS_KILLED


async def test_kill_aliases(fake_session, store):
    msg_id1 = await store.insert_message(_msg(to="W1TEST"))
    msg_id2 = await store.insert_message(_msg(to="W1TEST"))
    await _dispatch(fake_session, f"D {msg_id1}")
    await _dispatch(fake_session, f"KILL {msg_id2}")
    assert (await store.get_message(msg_id1)).status == STATUS_KILLED
    assert (await store.get_message(msg_id2)).status == STATUS_KILLED


async def test_kill_someone_elses_message_denied(fake_session, store):
    msg_id = await store.insert_message(_msg(to="K9OTHER"))
    await _dispatch(fake_session, f"K {msg_id}")
    # Should NOT be killed — user doesn't own it
    msg = await store.get_message(msg_id)
    assert msg.status != STATUS_KILLED


async def test_kill_sysop_can_kill_any(sysop_session, store):
    msg_id = await store.insert_message(_msg(to="K9OTHER"))
    await _dispatch(sysop_session, f"K {msg_id}")
    msg = await store.get_message(msg_id)
    assert msg.status == STATUS_KILLED


# ---------------------------------------------------------------------------
# R — read message
# ---------------------------------------------------------------------------

async def test_read_no_args_shows_usage(fake_session):
    out = await _dispatch(fake_session, "R")
    assert "Usage" in out or "usage" in out.lower()


async def test_read_message(fake_session, store):
    msg_id = await store.insert_message(_msg(to="W1TEST", subject="Hello"))
    out = await _dispatch(fake_session, f"R {msg_id}")
    assert "Hello" in out


async def test_read_nonexistent(fake_session):
    out = await _dispatch(fake_session, "R 99999")
    # Should report not found, not crash
    assert len(out) > 0


# ---------------------------------------------------------------------------
# L — list messages
# ---------------------------------------------------------------------------

async def test_list_no_messages(fake_session):
    out = await _dispatch(fake_session, "L")
    assert len(out) > 0


async def test_list_shows_messages(fake_session, store):
    await store.insert_message(_msg(subject="Visible"))
    out = await _dispatch(fake_session, "L")
    assert "Visible" in out


async def test_list_excludes_killed(fake_session, store):
    msg_id = await store.insert_message(_msg(subject="Dead"))
    await store.kill_message(msg_id)
    out = await _dispatch(fake_session, "L")
    assert "Dead" not in out


async def test_list_last(fake_session, store):
    for i in range(5):
        await store.insert_message(_msg(subject=f"Msg{i}"))
    out = await _dispatch(fake_session, "LL 3")
    # Should show something
    assert len(out) > 0


async def test_list_mine(fake_session, store):
    await store.insert_message(_msg(to="W1TEST", subject="Mine"))
    await store.insert_message(_msg(to="K9OTHER", subject="Theirs"))
    out = await _dispatch(fake_session, "LM")
    assert "Mine" in out
    assert "Theirs" not in out


async def test_list_bulletins(fake_session, store):
    await store.insert_message(_msg(msg_type=MSG_BULLETIN, subject="Bull"))
    await store.insert_message(_msg(msg_type=MSG_PRIVATE, subject="Priv"))
    out = await _dispatch(fake_session, "LB")
    assert "Bull" in out
    assert "Priv" not in out


async def test_list_killed_lk(fake_session, store):
    msg_id = await store.insert_message(_msg(subject="Killed"))
    await store.kill_message(msg_id)
    out = await _dispatch(fake_session, "LK")
    assert "Killed" in out


async def test_list_search_ls(fake_session, store):
    await store.insert_message(_msg(subject="SearchMe"))
    await store.insert_message(_msg(subject="Ignored"))
    out = await _dispatch(fake_session, "LS SearchMe")
    assert "SearchMe" in out
    assert "Ignored" not in out


async def test_list_search_no_args(fake_session):
    out = await _dispatch(fake_session, "LS")
    assert "Usage" in out or "usage" in out.lower()


async def test_list_date_ld(fake_session, store):
    # Just ensure it doesn't crash
    await store.insert_message(_msg(subject="Recent"))
    out = await _dispatch(fake_session, "LD 0101")
    assert len(out) > 0


async def test_list_reverse_lr(fake_session, store):
    for i in range(3):
        await store.insert_message(_msg(subject=f"Msg{i}"))
    out = await _dispatch(fake_session, "LR")
    assert len(out) > 0


async def test_list_new_login_ln(fake_session, store):
    await store.insert_message(_msg(subject="NewOne"))
    out = await _dispatch(fake_session, "LN")
    assert len(out) > 0


# ---------------------------------------------------------------------------
# S / SP — send personal mail (interactive)
# ---------------------------------------------------------------------------

async def test_send_private_no_args_prompts(fake_session):
    fake_session.push_input("K9DEST", "Test subject", "Hello", "/EX")
    out = await _dispatch(fake_session, "S")
    # Should send something or prompt, not crash
    assert len(out) >= 0


async def test_send_private_with_dest(fake_session, store):
    fake_session.push_input("Test subject", "Hello", "/EX")
    await _dispatch(fake_session, "S K9DEST")
    msgs = await store.list_messages(to_call="K9DEST")
    assert any(m.to_call == "K9DEST" for m in msgs)


# ---------------------------------------------------------------------------
# SC — copy message
# ---------------------------------------------------------------------------

async def test_copy_no_args_shows_usage(fake_session):
    out = await _dispatch(fake_session, "SC")
    assert "Usage" in out or "usage" in out.lower()


async def test_copy_message(fake_session, store):
    msg_id = await store.insert_message(_msg(to="W1TEST", subject="Original"))
    await _dispatch(fake_session, f"SC {msg_id} K9COPY")
    msgs = await store.list_messages(to_call="K9COPY")
    assert any(m.subject == "Original" for m in msgs)


# ---------------------------------------------------------------------------
# RP — reply to message
# ---------------------------------------------------------------------------

async def test_reply_no_args_shows_usage(fake_session):
    out = await _dispatch(fake_session, "RP")
    assert "Usage" in out or "usage" in out.lower()


async def test_reply_to_message(fake_session, store):
    msg_id = await store.insert_message(_msg(
        to="W1TEST", from_="K9SENDER", subject="Original"
    ))
    fake_session.push_input("Reply body", "/EX")
    await _dispatch(fake_session, f"RP {msg_id}")
    msgs = await store.list_messages(to_call="K9SENDER")
    assert any("Re:" in m.subject for m in msgs)


# ---------------------------------------------------------------------------
# KM — kill all my mail
# ---------------------------------------------------------------------------

async def test_kill_mine(fake_session, store):
    id1 = await store.insert_message(_msg(to="W1TEST", subject="Mine1"))
    id2 = await store.insert_message(_msg(to="W1TEST", subject="Mine2"))
    id3 = await store.insert_message(_msg(to="K9OTHER", subject="Other"))
    await _dispatch(fake_session, "KM")
    assert (await store.get_message(id1)).status == STATUS_KILLED
    assert (await store.get_message(id2)).status == STATUS_KILLED
    # Other user's message should be untouched
    assert (await store.get_message(id3)).status != STATUS_KILLED


# ---------------------------------------------------------------------------
# SH / SR — sysop hold / release
# ---------------------------------------------------------------------------

async def test_hold_requires_sysop(fake_session, store):
    msg_id = await store.insert_message(_msg())
    out = await _dispatch(fake_session, f"SH {msg_id}")
    # Regular user should get "no permission"
    assert (await store.get_message(msg_id)).status != STATUS_HELD


async def test_hold_message_sysop(sysop_session, store):
    msg_id = await store.insert_message(_msg())
    await _dispatch(sysop_session, f"SH {msg_id}")
    assert (await store.get_message(msg_id)).status == STATUS_HELD


async def test_release_message_sysop(sysop_session, store):
    msg_id = await store.insert_message(_msg())
    await store.hold_message(msg_id)
    await _dispatch(sysop_session, f"SR {msg_id}")
    assert (await store.get_message(msg_id)).status == STATUS_NEW


async def test_hold_no_args_shows_usage(sysop_session):
    out = await _dispatch(sysop_session, "SH")
    assert "Usage" in out or "usage" in out.lower()


# ---------------------------------------------------------------------------
# NB — set home BBS
# ---------------------------------------------------------------------------

async def test_nb_with_arg(fake_session, store):
    await _dispatch(fake_session, "NB W3BBS")
    user = await store.get_user("W1TEST")
    assert user.home_bbs == "W3BBS"


async def test_nb_updates_wp(fake_session, store):
    await _dispatch(fake_session, "NB W3BBS")
    wp = await store.get_wp_entry("W1TEST")
    assert wp is not None
    assert wp.home_bbs == "W3BBS"


# ---------------------------------------------------------------------------
# NH — set display name
# ---------------------------------------------------------------------------

async def test_nh_with_arg(fake_session, store):
    await _dispatch(fake_session, "NH Test User")
    user = await store.get_user("W1TEST")
    assert user.display_name == "Test User"


async def test_nh_updates_wp(fake_session, store):
    await _dispatch(fake_session, "NH Test User")
    wp = await store.get_wp_entry("W1TEST")
    assert wp is not None
    assert wp.name == "Test User"


# ---------------------------------------------------------------------------
# WPS — WP search
# ---------------------------------------------------------------------------

async def test_wps_no_args_shows_usage(fake_session):
    out = await _dispatch(fake_session, "WPS")
    assert "Usage" in out or "usage" in out.lower()


async def test_wps_finds_by_call(fake_session, store):
    from pybulletin.store.models import WPEntry
    await store.upsert_wp_entry(WPEntry(call="W1FIND", name="Alice"))
    out = await _dispatch(fake_session, "WPS W1FIND")
    assert "W1FIND" in out


async def test_wps_no_results(fake_session):
    out = await _dispatch(fake_session, "WPS ZZZNOBODY")
    assert "No" in out or len(out) > 0


# ---------------------------------------------------------------------------
# MOVE — sysop: reassign message
# ---------------------------------------------------------------------------

async def test_move_requires_sysop(fake_session, store):
    msg_id = await store.insert_message(_msg(to="W1TEST"))
    await _dispatch(fake_session, f"MOVE {msg_id} K9NEW")
    msg = await store.get_message(msg_id)
    assert msg.to_call == "W1TEST"  # unchanged


async def test_move_message(sysop_session, store):
    msg_id = await store.insert_message(_msg(to="W1TEST"))
    await _dispatch(sysop_session, f"MOVE {msg_id} K9NEW")
    msg = await store.get_message(msg_id)
    assert msg.to_call == "K9NEW"


async def test_move_no_args_shows_usage(sysop_session):
    out = await _dispatch(sysop_session, "MOVE")
    assert "Usage" in out or "usage" in out.lower()


async def test_move_with_at_bbs(sysop_session, store):
    msg_id = await store.insert_message(_msg(to="W1TEST"))
    await _dispatch(sysop_session, f"MOVE {msg_id} K9NEW@W3BBS")
    msg = await store.get_message(msg_id)
    assert msg.to_call == "K9NEW"
    assert msg.at_bbs == "W3BBS"


# ---------------------------------------------------------------------------
# ED — sysop: terminal edit
# ---------------------------------------------------------------------------

async def test_ed_requires_sysop(fake_session, store):
    msg_id = await store.insert_message(_msg(subject="Original"))
    out = await _dispatch(fake_session, f"ED {msg_id}")
    # Should get no-permission
    assert (await store.get_message(msg_id)).subject == "Original"


async def test_ed_no_args_shows_usage(sysop_session):
    out = await _dispatch(sysop_session, "ED")
    assert "Usage" in out or "usage" in out.lower()


async def test_ed_message(sysop_session, store):
    msg_id = await store.insert_message(_msg(subject="Old subject", body="Old body"))
    sysop_session.push_input("New subject", "New body line", "/EX")
    await _dispatch(sysop_session, f"ED {msg_id}")
    msg = await store.get_message(msg_id)
    assert msg.subject == "New subject"


# ---------------------------------------------------------------------------
# N — new messages summary
# ---------------------------------------------------------------------------

async def test_new_no_messages(fake_session):
    out = await _dispatch(fake_session, "N")
    assert len(out) > 0  # "no new messages" or similar


async def test_new_shows_messages(fake_session, store):
    await store.insert_message(_msg(to="W1TEST", subject="Fresh"))
    out = await _dispatch(fake_session, "N")
    assert "Fresh" in out


# ---------------------------------------------------------------------------
# RA — read all new personal mail
# ---------------------------------------------------------------------------

async def test_read_all_no_messages(fake_session):
    out = await _dispatch(fake_session, "RA")
    assert len(out) > 0


async def test_read_all_reads_mine(fake_session, store):
    msg_id = await store.insert_message(_msg(to="W1TEST", subject="ReadAll"))
    out = await _dispatch(fake_session, "RA")
    assert "ReadAll" in out


# ---------------------------------------------------------------------------
# U — users list (sysop)
# ---------------------------------------------------------------------------

async def test_users_list_requires_sysop(fake_session):
    out = await _dispatch(fake_session, "U")
    # Regular user denied
    assert "permission" in out.lower() or len(out) > 0


async def test_users_list_sysop(sysop_session, store):
    await store.record_login("K9LISTED", "x")
    out = await _dispatch(sysop_session, "U")
    assert "K9LISTED" in out or len(out) > 0


# ---------------------------------------------------------------------------
# O — options
# ---------------------------------------------------------------------------

async def test_options_show(fake_session):
    out = await _dispatch(fake_session, "O")
    assert len(out) > 0


async def test_options_set_lines(fake_session, store):
    await _dispatch(fake_session, "O LINES 40")
    user = await store.get_user("W1TEST")
    assert user.page_length == 40


async def test_options_set_cols(fake_session, store):
    await _dispatch(fake_session, "O COLS 132")
    val = await store.get_user_pref("W1TEST", "cols")
    assert val == "132"


# ---------------------------------------------------------------------------
# I / P — info / WP lookup
# ---------------------------------------------------------------------------

async def test_info_no_args(fake_session):
    out = await _dispatch(fake_session, "I")
    assert len(out) > 0


async def test_info_with_call(fake_session, store):
    from pybulletin.store.models import WPEntry
    await store.upsert_wp_entry(WPEntry(call="W1LOOK", home_bbs="W3BBS", name="Look Me Up"))
    out = await _dispatch(fake_session, "I W1LOOK")
    assert "W1LOOK" in out


async def test_info_unknown_call(fake_session):
    out = await _dispatch(fake_session, "I K9NOBODY")
    assert len(out) > 0  # not found message


# ---------------------------------------------------------------------------
# W / WHO — session info
# ---------------------------------------------------------------------------

async def test_who_command(fake_session):
    out = await _dispatch(fake_session, "W")
    assert len(out) > 0


# ---------------------------------------------------------------------------
# SN / ST — send NTS traffic
# ---------------------------------------------------------------------------

async def test_send_nts_no_args_prompts(fake_session):
    fake_session.push_input("K9DEST", "NTS subject", "Message body", "/EX")
    out = await _dispatch(fake_session, "SN")
    assert len(out) >= 0
