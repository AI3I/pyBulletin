from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from pybulletin.command.engine import CommandEngine
from pybulletin.config import load_config, save_config
from pybulletin import sysop_control


@pytest.mark.asyncio
async def test_user_cannot_control_services(fake_session, monkeypatch):
    set_web = AsyncMock()
    monkeypatch.setattr(sysop_control, "set_web", set_web)
    await CommandEngine(fake_session).dispatch("SYSOP WEB OFF")
    assert "permission" in fake_session.output().lower()
    set_web.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_login_cannot_control_services(sysop_session, monkeypatch):
    sysop_session.password_authenticated = False
    set_web = AsyncMock()
    monkeypatch.setattr(sysop_control, "set_web", set_web)
    await CommandEngine(sysop_session).dispatch("SYSOP WEB OFF")
    assert "permission" in sysop_session.output().lower()
    set_web.assert_not_awaited()


@pytest.mark.asyncio
async def test_sysop_web_command_uses_fixed_service_action(sysop_session, monkeypatch):
    set_web = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(sysop_control, "set_web", set_web)
    await CommandEngine(sysop_session).dispatch("SYSOP WEB OFF")
    set_web.assert_awaited_once_with(False)
    assert "web service off" in sysop_session.output().lower()


@pytest.mark.asyncio
async def test_sysop_status_reports_optional_features(sysop_session, monkeypatch):
    monkeypatch.setattr(sysop_control, "web_status", AsyncMock(return_value="stopped, disabled at boot"))
    await CommandEngine(sysop_session).dispatch("SYSOP STATUS")
    output = sysop_session.output().lower()
    assert "telnet     : running" in output
    assert "web        : stopped, disabled at boot" in output
    assert "radio      : disabled" in output


@pytest.mark.asyncio
async def test_sysop_forward_saves_and_restarts_core(sysop_session, monkeypatch):
    saved = []
    restart = AsyncMock()
    monkeypatch.setattr("pybulletin.config.save_config", lambda cfg, path: saved.append((cfg.forward.enabled, path)))
    monkeypatch.setattr(sysop_control, "restart_core_later", restart)
    await CommandEngine(sysop_session).dispatch("SYSOP FORWARD OFF")
    await asyncio.sleep(0)
    assert saved == [(False, sysop_session.cfg.source_path)]
    restart.assert_awaited_once()
    assert "disconnect" in sysop_session.output().lower()


@pytest.mark.asyncio
async def test_sysop_radio_requires_configured_modem(sysop_session, monkeypatch):
    sysop_session.cfg.kiss.device = ""
    restart = AsyncMock()
    monkeypatch.setattr(sysop_control, "restart_core_later", restart)
    await CommandEngine(sysop_session).dispatch("SYSOP RADIO KISS_SERIAL")
    assert "device" in sysop_session.output().lower()
    restart.assert_not_awaited()


def test_feature_settings_survive_config_reload(tmp_path):
    base = tmp_path / "pybulletin.toml"
    base.write_text("[node]\nnode_call = 'N0TEST'\n", encoding="utf-8")
    cfg = load_config(str(base))
    cfg.forward.enabled = False
    cfg.public_web.enabled = True
    cfg.beacon.enabled = True
    cfg.pactor.enabled = True
    cfg.kiss.transport = "kiss_tcp"
    save_config(cfg, cfg.source_path)
    loaded = load_config(str(base))
    assert not loaded.forward.enabled
    assert loaded.public_web.enabled
    assert loaded.beacon.enabled
    assert loaded.pactor.enabled
    assert loaded.kiss.transport == "kiss_tcp"
