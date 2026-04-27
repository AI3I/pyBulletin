from __future__ import annotations

import pytest

from pybulletin.config import AppConfig
from pybulletin.cli import _config_issues, _rf_diagnostics


def _cfg_with_transport(transport: str) -> AppConfig:
    cfg = AppConfig()
    cfg.kiss.transport = transport
    return cfg


@pytest.mark.asyncio
async def test_rf_diagnostics_disabled_reports_not_ready():
    cfg = _cfg_with_transport("disabled")

    lines = await _rf_diagnostics(cfg)

    assert "rf_ready         : no" in lines
    assert "reason           : [kiss].transport is disabled" in lines


@pytest.mark.asyncio
async def test_rf_diagnostics_kiss_tcp_requires_host():
    cfg = _cfg_with_transport("kiss_tcp")
    cfg.kiss.tcp_host = ""

    lines = await _rf_diagnostics(cfg)

    assert "rf_ready         : no" in lines
    assert "reason           : [kiss].tcp_host is empty" in lines


def test_config_validation_rejects_unknown_transport():
    cfg = _cfg_with_transport("kernel_ax25")

    issues = _config_issues(cfg)

    assert issues == ["[kiss].transport has unsupported value 'kernel_ax25'"]


def test_config_validation_rejects_bad_afsk_tones():
    cfg = _cfg_with_transport("afsk")
    cfg.afsk.mark_hz = 1200
    cfg.afsk.space_hz = 1200

    issues = _config_issues(cfg)

    assert "[afsk].mark_hz and [afsk].space_hz must differ" in issues


def test_config_validation_rejects_bad_ptt_selector():
    cfg = _cfg_with_transport("afsk")
    cfg.afsk.ptt_device = "cm108:/dev/hidraw0:not-a-pin"

    issues = _config_issues(cfg)

    assert any(issue.startswith("[afsk].ptt_device is invalid:") for issue in issues)


def test_config_validation_accepts_disabled_transport():
    cfg = _cfg_with_transport("disabled")

    assert _config_issues(cfg) == []
