"""Web SYSOP controls share configuration safely with the core service."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from pybulletin import sysop_control
from pybulletin.auth import hash_password
from pybulletin.config import load_config, update_config
from pybulletin.store.models import PRIV_SYSOP, User
from pybulletin.web.app import WebApp
from pybulletin.web.server import HTTPRequest


def _request(method: str, path: str, token: str = "", body: dict | None = None):
    return HTTPRequest(
        method=method, path=path, query="",
        headers={"authorization": f"Bearer {token}"} if token else {},
        body=json.dumps(body).encode() if body is not None else b"",
    )


@pytest.fixture
def configured_app(tmp_path, store, monkeypatch):
    base = tmp_path / "pybulletin.toml"
    base.write_text("[node]\nnode_call = 'N0TEST'\n", encoding="utf-8")
    monkeypatch.setattr(sysop_control, "restart_core_later", AsyncMock())
    return WebApp(load_config(str(base)), store), base


@pytest.mark.asyncio
async def test_neighbor_edit_preserves_telnet_feature_change(configured_app):
    app, base = configured_app
    session = await app._sessions.create("N0SYS", PRIV_SYSOP)
    update_config(str(base), lambda cfg: setattr(cfg.forward, "enabled", False))
    response = await app.handle_request(_request(
        "POST", "/api/neighbors", session.token,
        {"call": "B1BBB", "address": "B1BBB", "protocol": "ascii"},
    ))
    assert response.status == 201
    current = load_config(str(base))
    assert not current.forward.enabled
    assert [n.call for n in current.forward.neighbors] == ["B1BBB"]
    await asyncio.sleep(0)
    sysop_control.restart_core_later.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_change_requires_current_password(configured_app, store):
    app, base = configured_app
    await store.upsert_user(User(
        call="N0SYS", privilege=PRIV_SYSOP,
        password_hash=hash_password("secret123"),
    ))
    session = await app._sessions.create("N0SYS", PRIV_SYSOP)
    bad = await app.handle_request(_request(
        "POST", "/api/sysop/control", session.token,
        {"feature": "FORWARD", "action": "OFF", "password": "wrong"},
    ))
    assert bad.status == 400
    assert load_config(str(base)).forward.enabled

    good = await app.handle_request(_request(
        "POST", "/api/sysop/control", session.token,
        {"feature": "FORWARD", "action": "OFF", "password": "secret123"},
    ))
    assert good.status == 200
    assert not load_config(str(base)).forward.enabled
    await asyncio.sleep(0)
    sysop_control.restart_core_later.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_ui_off_blocks_root_and_direct_index(configured_app):
    app, base = configured_app
    for path in ("/", "/index.html"):
        assert (await app.handle_request(_request("GET", path))).status == 404
    update_config(str(base), lambda cfg: setattr(cfg.public_web, "enabled", True))
    assert (await app.handle_request(_request("GET", "/"))).status == 200


@pytest.mark.asyncio
async def test_service_status_reports_live_and_configured_state(configured_app, monkeypatch):
    app, base = configured_app
    monkeypatch.setattr(sysop_control, "core_running", AsyncMock(return_value=True))
    monkeypatch.setattr(sysop_control, "web_status", AsyncMock(return_value="running, enabled at boot"))
    session = await app._sessions.create("N0SYS", PRIV_SYSOP)
    response = await app.handle_request(_request("GET", "/api/sysop/status", session.token))
    data = json.loads(response.body)
    assert data["core_running"] is True
    assert data["web"] == "running, enabled at boot"
    assert data["radio"] == "disabled"
    assert data["public"] is False
