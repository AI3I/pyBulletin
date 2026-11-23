"""Local-only manual forwarding requests from the web service to the core."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

LOG = logging.getLogger(__name__)


def control_path(cfg) -> Path:
    return Path(cfg.store.sqlite_path).resolve().with_suffix(".forward.sock")


async def start_control_server(cfg, scheduler):
    path = control_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=1,
            )
        except (OSError, asyncio.TimeoutError):
            pass  # A socket left by a stopped core can be replaced.
        else:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError(f"forward control socket already active: {path}")
        path.unlink()

    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(line)
            call = str(request.get("call", "")).upper()
            if not call or len(call) > 10:
                raise ValueError("invalid neighbor callsign")
            sent, received = await scheduler.connect_now(call)
            result = {"ok": True, "sent": sent, "received": received}
        except Exception as exc:
            LOG.warning("forward control: request failed: %s", exc)
            result = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(result) + "\n").encode("utf-8"))
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path), limit=4096)
    path.chmod(0o600)
    LOG.info("forward control: listening on %s", path)
    return server


async def stop_control_server(server, cfg) -> None:
    server.close()
    await server.wait_closed()
    control_path(cfg).unlink(missing_ok=True)


async def request_connect(cfg, call: str) -> tuple[int, int]:
    path = control_path(cfg)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=3,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise ConnectionError("forwarding core is unavailable; restart pybulletin.service") from exc
    try:
        writer.write((json.dumps({"call": call}) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=120)
        if not line:
            raise ConnectionError("forwarding core closed the control connection")
        result = json.loads(line)
        if not result.get("ok"):
            raise ConnectionError(str(result.get("error", "forwarding failed")))
        return int(result["sent"]), int(result["received"])
    finally:
        writer.close()
        await writer.wait_closed()
