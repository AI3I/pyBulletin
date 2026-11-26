"""Narrow systemd operations available to an authenticated SYSOP session."""
from __future__ import annotations

import asyncio
import logging

LOG = logging.getLogger(__name__)


async def _run(*args: str) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except OSError as exc:
        return False, str(exc)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, "systemd command timed out"
    detail = (stderr or stdout).decode(errors="replace").strip()
    return proc.returncode == 0, detail


async def web_status() -> str:
    active, _ = await _run("systemctl", "is-active", "pybulletinweb.service")
    enabled, _ = await _run("systemctl", "is-enabled", "pybulletinweb.service")
    return f"{'running' if active else 'stopped'}, {'enabled' if enabled else 'disabled'} at boot"


async def web_running() -> bool:
    active, _ = await _run("systemctl", "is-active", "pybulletinweb.service")
    return active


async def core_running() -> bool:
    active, _ = await _run("systemctl", "is-active", "pybulletin.service")
    return active


async def set_web(enabled: bool) -> tuple[bool, str]:
    action = "enable" if enabled else "disable"
    return await _run("sudo", "-n", "/usr/bin/systemctl", action, "--now", "pybulletinweb.service")


async def restart_web() -> tuple[bool, str]:
    return await _run("sudo", "-n", "/usr/bin/systemctl", "restart", "pybulletinweb.service")


async def restart_core_later() -> None:
    await asyncio.sleep(1)
    ok, detail = await _run("sudo", "-n", "/usr/bin/systemctl", "restart", "pybulletin.service")
    if not ok:
        LOG.error("SYSOP core restart failed: %s", detail)
