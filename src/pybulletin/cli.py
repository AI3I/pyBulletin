from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .config import load_config

LOG = logging.getLogger(__name__)


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        level=level,
        stream=sys.stdout,
    )


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pybulletin", description=f"pyBulletin {__version__}")
    ap.add_argument("--config", default="config/pybulletin.toml", metavar="PATH")
    ap.add_argument("--debug", action="store_true")

    sub = ap.add_subparsers(dest="command")
    sub.add_parser("serve",      help="Start all services in one process (recommended)")
    sub.add_parser("serve-core", help="Start the core BBS service only (telnet + transports)")
    sub.add_parser("serve-web",  help="Start the web service only (public UI + sysop console)")
    sub.add_parser("run-forward",   help="Run one forwarding cycle and exit")
    sub.add_parser("run-retention", help="Run message retention cleanup and exit")
    sub.add_parser("doctor",     help="Print deployment health summary")
    rf = sub.add_parser("doctor-rf",  help="Inspect userspace RF transport readiness")
    rf.add_argument("--connect-timeout", type=float, default=1.0,
                    help="Seconds to wait when probing KISS TCP endpoints")
    sub.add_parser("doctor-afsk", help="Inspect native Bell 202 audio/PTT configuration and device support")
    sub.add_parser("validate-config", help="Validate configuration and exit non-zero on errors")
    ptt = sub.add_parser("test-ptt", help="Key configured AFSK PTT briefly, then release it")
    ptt.add_argument("--selector", default="", help="Override [afsk].ptt_device for this test")
    ptt.add_argument("--duration", type=float, default=1.0, help="Seconds to key PTT, 0.1 to 10.0")
    return ap


async def _serve_core(config_path: str) -> None:
    from .store.store import BBSStore
    from .strings import StringCatalog
    from .transport.telnet import TelnetServer
    from .transport.kiss_tcp import KissTcpLink
    from .transport.kiss_serial import KissSerialLink
    from .transport.afsk import AfskBell202Link
    from .transport.conference import ConferenceHubManager
    from .ax25.router import AX25Router
    from .ax25.beacon import BeaconTask
    from .session.session import BBSSession

    cfg = load_config(config_path)
    LOG.info("pyBulletin %s starting — node %s", __version__, cfg.node.node_call)

    store   = BBSStore(cfg.store.sqlite_path)
    strings = StringCatalog("config/strings.toml")
    conf_hub = ConferenceHubManager()

    # --- Telnet / TCP transport ---
    async def _session_handler(reader, writer, meta):
        session = BBSSession(
            reader, writer, meta, cfg, store, strings,
            conference_hub=conf_hub,
        )
        await session.run()

    server = TelnetServer(
        cfg.telnet.host,
        cfg.telnet.port,
        _session_handler,
        max_clients=cfg.telnet.max_clients,
        idle_timeout=float(cfg.telnet.idle_timeout_seconds),
    )
    await server.start()

    extra_servers: list[TelnetServer] = []
    for port in cfg.telnet.ports:
        if port != cfg.telnet.port:
            s = TelnetServer(
                cfg.telnet.host, port, _session_handler,
                max_clients=cfg.telnet.max_clients,
                idle_timeout=float(cfg.telnet.idle_timeout_seconds),
            )
            await s.start()
            extra_servers.append(s)

    # --- B2F inbound forward listener ---
    fwd_server = None
    if cfg.forward.enabled and cfg.forward.listen_port:
        from .forward.session import ForwardSession
        from .forward.sid import parse as parse_sid
        from .config import ForwardNeighborConfig

        async def _forward_handler(reader, writer):
            peer = writer.get_extra_info("peername", ("?", 0))
            LOG.info("forward: inbound connection from %s:%s", *peer)
            try:
                sid_line = await asyncio.wait_for(reader.readline(), timeout=15.0)
            except asyncio.TimeoutError:
                writer.close()
                return
            sid_str = sid_line.decode("ascii", errors="replace").strip()
            remote_sid = parse_sid(sid_str)
            remote_call = (remote_sid.call if remote_sid else "").upper()

            neighbor = next(
                (n for n in cfg.forward.neighbors
                 if n.call.upper() == remote_call and n.enabled),
                ForwardNeighborConfig(call=remote_call or "UNKNOWN"),
            )

            # Shim: replay the already-read SID line before the live reader
            class _PrefixedReader:
                def __init__(self, prefix: bytes, inner):
                    self._buf = prefix
                    self._inner = inner
                async def readline(self_inner):
                    if self_inner._buf:
                        line, self_inner._buf = self_inner._buf, b""
                        return line
                    return await self_inner._inner.readline()
                async def readexactly(self_inner, n):
                    return await self_inner._inner.readexactly(n)
                def at_eof(self_inner):
                    return self_inner._inner.at_eof()

            sess = ForwardSession(cfg, store, neighbor)
            sess._reader = _PrefixedReader(sid_line, reader)
            sess._writer = writer
            try:
                await sess._run(caller=False)
            except Exception as exc:
                LOG.warning("forward: inbound session error: %s", exc)
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        fwd_server = await asyncio.start_server(
            _forward_handler,
            cfg.forward.listen_host,
            cfg.forward.listen_port,
        )
        LOG.info("serve-core: B2F forward listener on %s:%d",
                 cfg.forward.listen_host, cfg.forward.listen_port)

    # --- AX.25 / KISS transport ---
    ax25_link = None
    beacon    = None

    async def _send_ax25(frame, port=0):
        if ax25_link:
            await ax25_link.send_frame(frame, port)

    router = AX25Router(cfg, store, strings, _send_ax25, conference_hub=conf_hub)

    kiss = cfg.kiss
    transport = kiss.transport
    if transport == "afsk":
        afsk = cfg.afsk
        ax25_link = AfskBell202Link(afsk, router)
        ax25_link.start()
        LOG.info(
            "serve-core: direct Bell 202 AFSK enabled "
            "(input=%s output=%s sample_rate=%d)",
            afsk.input_device or "<default>",
            afsk.output_device or "<default>",
            afsk.sample_rate,
        )
    elif transport == "kiss_serial":
        ax25_link = KissSerialLink(
            kiss.device, kiss.baud, router,
            init_cmds=list(kiss.init_cmds),
            init_delay_ms=kiss.init_delay_ms,
        )
        ax25_link.start()
        LOG.info("serve-core: KISS serial on %s at %d baud", kiss.device, kiss.baud)
        if kiss.init_cmds:
            LOG.info("serve-core: TNC init sequence: %s", kiss.init_cmds)
    elif transport == "kiss_tcp":
        ax25_link = KissTcpLink(kiss.tcp_host, kiss.tcp_port, router)
        ax25_link.start()
        LOG.info("serve-core: KISS TCP → %s:%d", kiss.tcp_host, kiss.tcp_port)
    elif transport == "disabled":
        LOG.info("serve-core: AX.25 transport disabled — RF disabled")
    else:
        LOG.warning("serve-core: unknown AX.25 transport %r — RF disabled", transport)

    if ax25_link and cfg.beacon.enabled:
        beacon = BeaconTask(router, cfg)
        beacon.start()
        LOG.info("serve-core: beacon enabled — %s", cfg.beacon.text)

    # --- PACTOR ---
    pactor_link = None
    if cfg.pactor.enabled:
        from .transport.pactor import PactorLink
        pactor_link = PactorLink(
            cfg.pactor.device, cfg.pactor.baud, router,
            paclen=cfg.pactor.paclen,
        )
        pactor_link.start()
        LOG.info("serve-core: PACTOR on %s at %d baud", cfg.pactor.device, cfg.pactor.baud)

    # --- Periodic forward scheduler ---
    fwd_task = None
    if cfg.forward.enabled:
        from .forward.scheduler import ForwardScheduler

        fwd_scheduler = ForwardScheduler(cfg, store)

        async def _fwd_loop():
            import time as _time
            sleep = 60 - (_time.time() % 60)
            await asyncio.sleep(sleep)
            while True:
                try:
                    await fwd_scheduler.run_once()
                except Exception as exc:
                    LOG.warning("serve-core: forward scheduler error: %s", exc)
                await asyncio.sleep(60)

        fwd_task = asyncio.create_task(_fwd_loop(), name="forward-scheduler")
        LOG.info("serve-core: forward scheduler started")

    # --- Run until signal ---
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    LOG.info("serve-core: running — waiting for SIGINT/SIGTERM")
    await stop.wait()
    LOG.info("serve-core: shutting down")

    if fwd_task:
        fwd_task.cancel()
        try:
            await fwd_task
        except asyncio.CancelledError:
            pass
    if beacon:
        beacon.stop()
    if pactor_link:
        await pactor_link.stop()
    if ax25_link:
        await ax25_link.stop()
    if fwd_server:
        fwd_server.close()
        await fwd_server.wait_closed()
    await server.stop()
    for s in extra_servers:
        await s.stop()
    await store.close()


async def _serve_web(config_path: str) -> None:
    from pathlib import Path
    from .store.store import BBSStore
    from .web.server import HTTPServer
    from .web.app import WebApp

    cfg = load_config(config_path)
    LOG.info("pyBulletin %s web starting — node %s", __version__, cfg.node.node_call)

    store  = BBSStore(cfg.store.sqlite_path)
    app    = WebApp(cfg, store)
    app.start()

    static_dir = Path(__file__).parent / "web" / "static"
    server = HTTPServer(
        cfg.web.host,
        cfg.web.port,
        app.handle_request,
        ws_handler=app.handle_ws,
        static_dir=static_dir,
    )
    await server.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    LOG.info("serve-web: running on %s:%d", cfg.web.host, cfg.web.port)
    await stop.wait()
    LOG.info("serve-web: shutting down")

    await server.stop()
    app.stop()
    await store.close()


async def _cmd_doctor(config_path: str) -> None:
    from pathlib import Path
    cfg = load_config(config_path)
    print(f"pyBulletin {__version__}")
    print(f"  node      : {cfg.node.node_call}")
    print(f"  qth       : {cfg.node.qth}")
    print(f"  telnet    : {cfg.telnet.host}:{cfg.telnet.port}")
    print(f"  web       : {cfg.web.host}:{cfg.web.port}")
    print(f"  public    : {'enabled' if cfg.public_web.enabled else 'disabled'}")
    print(f"  db        : {cfg.store.sqlite_path}")
    db_exists = Path(cfg.store.sqlite_path).exists()
    print(f"  db exists : {db_exists}")
    if db_exists:
        from .store.store import BBSStore
        store = BBSStore(cfg.store.sqlite_path)
        print(f"  messages  : {await store.count_messages()}")
        print(f"  users     : {len(await store.list_users())}")
        print(f"  wp entries: {await store.count_wp_entries()}")
        await store.close()


async def _cmd_doctor_afsk(config_path: str) -> None:
    cfg = load_config(config_path)
    from .transport.afsk import afsk_diagnostics

    print(f"pyBulletin {__version__}")
    print(f"  node             : {cfg.node.node_call}")
    print(f"  selected         : {cfg.kiss.transport}")
    for line in afsk_diagnostics(cfg.afsk):
        key, _, value = line.partition(":")
        print(f"  {key:<16}: {value.strip()}")


async def _cmd_doctor_rf(config_path: str, connect_timeout: float = 1.0) -> None:
    cfg = load_config(config_path)
    print(f"pyBulletin {__version__}")
    print(f"  node             : {cfg.node.node_call}")
    print("  kernel_ax25      : not required")
    print(f"  selected         : {cfg.kiss.transport}")
    for line in await _rf_diagnostics(cfg, connect_timeout=connect_timeout):
        key, _, value = line.partition(":")
        print(f"  {key:<16}: {value.strip()}")


async def _rf_diagnostics(cfg, *, connect_timeout: float = 1.0) -> list[str]:
    from pathlib import Path

    lines: list[str] = []
    transport = cfg.kiss.transport
    if transport == "disabled":
        lines.append("rf_ready         : no")
        lines.append("reason           : [kiss].transport is disabled")
        return lines
    if transport == "kiss_tcp":
        if not cfg.kiss.tcp_host:
            lines.append("rf_ready         : no")
            lines.append("reason           : [kiss].tcp_host is empty")
        else:
            lines.append("rf_ready         : maybe")
            lines.append(f"kiss_tcp         : {cfg.kiss.tcp_host}:{cfg.kiss.tcp_port}")
            ok, detail = await _probe_tcp(cfg.kiss.tcp_host, cfg.kiss.tcp_port, connect_timeout)
            if ok:
                lines.append("kiss_tcp_connect : ok")
                lines.append("next_check       : verify radio/PTT/audio on the KISS modem")
            else:
                lines[0] = "rf_ready         : no"
                lines.append(f"kiss_tcp_connect : failed ({detail})")
        return lines
    if transport == "kiss_serial":
        if not cfg.kiss.device:
            lines.append("rf_ready         : no")
            lines.append("reason           : [kiss].device is empty")
        elif not Path(cfg.kiss.device).exists():
            lines.append("rf_ready         : no")
            lines.append(f"reason           : serial device missing ({cfg.kiss.device})")
        else:
            lines.append("rf_ready         : maybe")
            lines.append(f"kiss_serial      : {cfg.kiss.device} @ {cfg.kiss.baud}")
        try:
            import serial  # type: ignore[import]  # noqa: F401
        except Exception:
            lines.append("pyserial         : missing")
        else:
            lines.append("pyserial         : available")
        try:
            import serial_asyncio  # type: ignore[import]  # noqa: F401
        except Exception:
            lines.append("serial_asyncio   : missing")
        else:
            lines.append("serial_asyncio   : available")
        return lines
    if transport == "afsk":
        from .transport.afsk import afsk_diagnostics

        lines.append("rf_ready         : maybe")
        lines.extend(afsk_diagnostics(cfg.afsk))
        return lines
    lines.append("rf_ready         : no")
    lines.append(f"reason           : unknown transport {transport!r}")
    return lines


async def _probe_tcp(host: str, port: int, timeout: float) -> tuple[bool, str]:
    timeout = max(0.1, min(10.0, float(timeout)))
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except Exception as exc:
        return False, str(exc) or exc.__class__.__name__
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True, "connected"


def _config_issues(cfg) -> list[str]:
    from pathlib import Path

    issues: list[str] = []
    transport = cfg.kiss.transport
    if transport not in {"disabled", "kiss_tcp", "kiss_serial", "afsk"}:
        issues.append(f"[kiss].transport has unsupported value {transport!r}")
        return issues
    if transport == "kiss_tcp":
        if not cfg.kiss.tcp_host:
            issues.append("[kiss].tcp_host is required when transport = kiss_tcp")
        if not (1 <= int(cfg.kiss.tcp_port) <= 65535):
            issues.append("[kiss].tcp_port must be between 1 and 65535")
    elif transport == "kiss_serial":
        if not cfg.kiss.device:
            issues.append("[kiss].device is required when transport = kiss_serial")
        elif not Path(cfg.kiss.device).exists():
            issues.append(f"[kiss].device does not exist: {cfg.kiss.device}")
        if int(cfg.kiss.baud) <= 0:
            issues.append("[kiss].baud must be positive")
    elif transport == "afsk":
        if int(cfg.afsk.sample_rate) <= 0:
            issues.append("[afsk].sample_rate must be positive")
        if int(cfg.afsk.baud) <= 0:
            issues.append("[afsk].baud must be positive")
        if int(cfg.afsk.mark_hz) <= 0 or int(cfg.afsk.space_hz) <= 0:
            issues.append("[afsk].mark_hz and [afsk].space_hz must be positive")
        if cfg.afsk.mark_hz == cfg.afsk.space_hz:
            issues.append("[afsk].mark_hz and [afsk].space_hz must differ")
        if cfg.afsk.ptt_device:
            try:
                from .transport.afsk import _parse_ptt_selector

                _parse_ptt_selector(cfg.afsk.ptt_device)
            except Exception as exc:
                issues.append(f"[afsk].ptt_device is invalid: {exc}")
    return issues


async def _cmd_validate_config(config_path: str) -> None:
    cfg = load_config(config_path)
    issues = _config_issues(cfg)
    print(f"pyBulletin {__version__}")
    print(f"  node             : {cfg.node.node_call}")
    if not issues:
        print("  config           : ok")
        return
    print("  config           : invalid")
    for issue in issues:
        print(f"  issue            : {issue}")
    raise SystemExit(1)


async def _cmd_test_ptt(config_path: str, selector: str, duration: float) -> None:
    cfg = load_config(config_path)
    from .transport.afsk import afsk_test_ptt

    selected = selector or cfg.afsk.ptt_device
    print(f"pyBulletin {__version__}")
    print(f"  node             : {cfg.node.node_call}")
    for line in await afsk_test_ptt(selected, duration):
        key, _, value = line.partition(":")
        print(f"  {key:<16}: {value.strip()}")


async def _cmd_run_forward(config_path: str) -> None:
    from .store.store import BBSStore
    from .forward.scheduler import ForwardScheduler

    cfg   = load_config(config_path)
    store = BBSStore(cfg.store.sqlite_path)
    try:
        scheduler = ForwardScheduler(cfg, store)
        sent, received = await scheduler.run_once()
        LOG.info("run-forward: done — sent %d, received %d", sent, received)
    finally:
        await store.close()


async def _cmd_run_retention(config_path: str) -> None:
    cfg = load_config(config_path)
    from .store.store import BBSStore
    store = BBSStore(cfg.store.sqlite_path)
    r = cfg.retention
    removed = await store.cleanup_expired(
        personal_days=r.personal_mail_days,
        bulletin_days=r.bulletin_days,
        nts_days=r.nts_days,
        killed_days=r.killed_days,
    )
    await store.close()
    LOG.info("retention: removed %d expired message(s)", removed)


async def _serve(config_path: str) -> None:
    """Combined core + web in one event loop — enables live conference monitor."""
    from pathlib import Path
    from .store.store import BBSStore
    from .strings import StringCatalog
    from .transport.telnet import TelnetServer
    from .transport.kiss_tcp import KissTcpLink
    from .transport.kiss_serial import KissSerialLink
    from .transport.afsk import AfskBell202Link
    from .transport.conference import ConferenceHubManager
    from .ax25.router import AX25Router
    from .ax25.beacon import BeaconTask
    from .session.session import BBSSession
    from .web.server import HTTPServer
    from .web.app import WebApp

    cfg = load_config(config_path)
    LOG.info("pyBulletin %s starting — node %s", __version__, cfg.node.node_call)

    store    = BBSStore(cfg.store.sqlite_path)
    strings  = StringCatalog("config/strings.toml")
    conf_hub = ConferenceHubManager()

    # --- Web app (shares conf_hub) ---
    app = WebApp(cfg, store, conference_hub=conf_hub)
    app.start()
    static_dir = Path(__file__).parent / "web" / "static"
    web_server = HTTPServer(
        cfg.web.host, cfg.web.port,
        app.handle_request,
        ws_handler=app.handle_ws,
        static_dir=static_dir,
    )
    await web_server.start()
    LOG.info("serve: web on %s:%d", cfg.web.host, cfg.web.port)

    # Wire conference state changes to WebSocket broadcast
    async def _conf_broadcast():
        snapshot = conf_hub.rooms_snapshot()
        rooms_out = {n: {"members": m, "count": len(m)} for n, m in snapshot.items()}
        await app.broadcast({"type": "conference_update",
                             "available": True, "rooms": rooms_out})

    def _on_conf_change():
        asyncio.get_event_loop().create_task(_conf_broadcast())

    conf_hub.set_state_change_callback(_on_conf_change)

    # --- Telnet / TCP transport ---
    async def _session_handler(reader, writer, meta):
        session = BBSSession(
            reader, writer, meta, cfg, store, strings,
            conference_hub=conf_hub,
        )
        await session.run()

    server = TelnetServer(
        cfg.telnet.host, cfg.telnet.port, _session_handler,
        max_clients=cfg.telnet.max_clients,
        idle_timeout=float(cfg.telnet.idle_timeout_seconds),
    )
    await server.start()

    extra_servers: list[TelnetServer] = []
    for port in cfg.telnet.ports:
        if port != cfg.telnet.port:
            s = TelnetServer(
                cfg.telnet.host, port, _session_handler,
                max_clients=cfg.telnet.max_clients,
                idle_timeout=float(cfg.telnet.idle_timeout_seconds),
            )
            await s.start()
            extra_servers.append(s)

    # --- B2F inbound forward listener ---
    fwd_server = None
    if cfg.forward.enabled and cfg.forward.listen_port:
        from .forward.session import ForwardSession
        from .forward.sid import parse as parse_sid
        from .config import ForwardNeighborConfig

        async def _forward_handler(reader, writer):
            peer = writer.get_extra_info("peername", ("?", 0))
            LOG.info("forward: inbound connection from %s:%s", *peer)
            try:
                sid_line = await asyncio.wait_for(reader.readline(), timeout=15.0)
            except asyncio.TimeoutError:
                writer.close()
                return
            sid_str = sid_line.decode("ascii", errors="replace").strip()
            remote_sid = parse_sid(sid_str)
            remote_call = (remote_sid.call if remote_sid else "").upper()
            neighbor = next(
                (n for n in cfg.forward.neighbors
                 if n.call.upper() == remote_call and n.enabled),
                ForwardNeighborConfig(call=remote_call or "UNKNOWN"),
            )

            class _PrefixedReader:
                def __init__(self, prefix, inner):
                    self._buf = prefix
                    self._inner = inner
                async def readline(self_inner):
                    if self_inner._buf:
                        line, self_inner._buf = self_inner._buf, b""
                        return line
                    return await self_inner._inner.readline()
                async def readexactly(self_inner, n):
                    return await self_inner._inner.readexactly(n)
                def at_eof(self_inner):
                    return self_inner._inner.at_eof()

            sess = ForwardSession(cfg, store, neighbor)
            sess._reader = _PrefixedReader(sid_line, reader)
            sess._writer = writer
            try:
                await sess._run(caller=False)
            except Exception as exc:
                LOG.warning("forward: inbound session error: %s", exc)
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        fwd_server = await asyncio.start_server(
            _forward_handler, cfg.forward.listen_host, cfg.forward.listen_port,
        )
        LOG.info("serve: B2F forward listener on %s:%d",
                 cfg.forward.listen_host, cfg.forward.listen_port)

    # --- AX.25 / KISS transport ---
    ax25_link = None
    beacon    = None
    pactor_link = None

    async def _send_ax25(frame, port=0):
        if ax25_link:
            await ax25_link.send_frame(frame, port)

    router = AX25Router(cfg, store, strings, _send_ax25, conference_hub=conf_hub)

    kiss = cfg.kiss
    transport = kiss.transport
    if transport == "afsk":
        afsk = cfg.afsk
        ax25_link = AfskBell202Link(afsk, router)
        ax25_link.start()
        LOG.info(
            "serve: direct Bell 202 AFSK enabled "
            "(input=%s output=%s sample_rate=%d)",
            afsk.input_device or "<default>",
            afsk.output_device or "<default>",
            afsk.sample_rate,
        )
    elif transport == "kiss_serial":
        ax25_link = KissSerialLink(
            kiss.device, kiss.baud, router,
            init_cmds=list(kiss.init_cmds),
            init_delay_ms=kiss.init_delay_ms,
        )
        ax25_link.start()
        LOG.info("serve: KISS serial on %s at %d baud", kiss.device, kiss.baud)
    elif transport == "kiss_tcp":
        ax25_link = KissTcpLink(kiss.tcp_host, kiss.tcp_port, router)
        ax25_link.start()
        LOG.info("serve: KISS TCP → %s:%d", kiss.tcp_host, kiss.tcp_port)
    elif transport == "disabled":
        LOG.info("serve: AX.25 transport disabled — RF disabled")
    else:
        LOG.warning("serve: unknown AX.25 transport %r — RF disabled", transport)

    if ax25_link and cfg.beacon.enabled:
        beacon = BeaconTask(router, cfg)
        beacon.start()

    if cfg.pactor.enabled:
        from .transport.pactor import PactorLink
        pactor_link = PactorLink(
            cfg.pactor.device, cfg.pactor.baud, router,
            paclen=cfg.pactor.paclen,
        )
        pactor_link.start()

    # --- Periodic forward scheduler ---
    fwd_task = None
    if cfg.forward.enabled:
        from .forward.scheduler import ForwardScheduler

        fwd_scheduler = ForwardScheduler(cfg, store)

        async def _fwd_loop():
            # Align to the next whole minute, then tick every 60 s — mirrors cron.
            import time as _time
            sleep = 60 - (_time.time() % 60)
            await asyncio.sleep(sleep)
            while True:
                try:
                    await fwd_scheduler.run_once()
                except Exception as exc:
                    LOG.warning("serve: forward scheduler error: %s", exc)
                await asyncio.sleep(60)

        fwd_task = asyncio.create_task(_fwd_loop(), name="forward-scheduler")
        LOG.info("serve: forward scheduler started")

    # --- Run until signal ---
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    LOG.info("serve: running — waiting for SIGINT/SIGTERM")
    await stop.wait()
    LOG.info("serve: shutting down")

    if fwd_task:
        fwd_task.cancel()
        try:
            await fwd_task
        except asyncio.CancelledError:
            pass
    if beacon:
        beacon.stop()
    if pactor_link:
        await pactor_link.stop()
    if ax25_link:
        await ax25_link.stop()
    if fwd_server:
        fwd_server.close()
        await fwd_server.wait_closed()
    await server.stop()
    for s in extra_servers:
        await s.stop()
    await web_server.stop()
    app.stop()
    await store.close()


def main() -> None:
    ap = _build_parser()
    args = ap.parse_args()
    _setup_logging(args.debug)

    if not args.command:
        ap.print_help()
        sys.exit(0)

    if args.command == "serve":
        asyncio.run(_serve(args.config))
    elif args.command == "serve-core":
        asyncio.run(_serve_core(args.config))
    elif args.command == "serve-web":
        asyncio.run(_serve_web(args.config))
    elif args.command == "doctor":
        asyncio.run(_cmd_doctor(args.config))
    elif args.command == "doctor-rf":
        asyncio.run(_cmd_doctor_rf(args.config, args.connect_timeout))
    elif args.command == "doctor-afsk":
        asyncio.run(_cmd_doctor_afsk(args.config))
    elif args.command == "validate-config":
        asyncio.run(_cmd_validate_config(args.config))
    elif args.command == "test-ptt":
        asyncio.run(_cmd_test_ptt(args.config, args.selector, args.duration))
    elif args.command == "run-forward":
        asyncio.run(_cmd_run_forward(args.config))
    elif args.command == "run-retention":
        asyncio.run(_cmd_run_retention(args.config))
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
