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
    return ap


async def _serve_core(config_path: str) -> None:
    from .store.store import BBSStore
    from .strings import StringCatalog
    from .transport.telnet import TelnetServer
    from .transport.kiss_tcp import KissTcpLink
    from .transport.kiss_serial import KissSerialLink
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
    kiss_link = None
    beacon    = None

    async def _send_ax25(frame, port=0):
        if kiss_link:
            await kiss_link.send_frame(frame, port)

    router = AX25Router(cfg, store, strings, _send_ax25, conference_hub=conf_hub)

    kiss = cfg.kiss
    if kiss.device:
        kiss_link = KissSerialLink(
            kiss.device, kiss.baud, router,
            init_cmds=list(kiss.init_cmds),
            init_delay_ms=kiss.init_delay_ms,
        )
        kiss_link.start()
        LOG.info("serve-core: KISS serial on %s at %d baud", kiss.device, kiss.baud)
        if kiss.init_cmds:
            LOG.info("serve-core: TNC init sequence: %s", kiss.init_cmds)
    elif kiss.tcp_host:
        kiss_link = KissTcpLink(kiss.tcp_host, kiss.tcp_port, router)
        kiss_link.start()
        LOG.info("serve-core: KISS TCP → %s:%d", kiss.tcp_host, kiss.tcp_port)
    else:
        LOG.info("serve-core: no KISS TNC configured — RF disabled")

    if kiss_link and cfg.beacon.enabled:
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
    if kiss_link:
        await kiss_link.stop()
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
    kiss_link = None
    beacon    = None
    pactor_link = None

    async def _send_ax25(frame, port=0):
        if kiss_link:
            await kiss_link.send_frame(frame, port)

    router = AX25Router(cfg, store, strings, _send_ax25, conference_hub=conf_hub)

    kiss = cfg.kiss
    if kiss.device:
        kiss_link = KissSerialLink(
            kiss.device, kiss.baud, router,
            init_cmds=list(kiss.init_cmds),
            init_delay_ms=kiss.init_delay_ms,
        )
        kiss_link.start()
        LOG.info("serve: KISS serial on %s at %d baud", kiss.device, kiss.baud)
    elif kiss.tcp_host:
        kiss_link = KissTcpLink(kiss.tcp_host, kiss.tcp_port, router)
        kiss_link.start()
        LOG.info("serve: KISS TCP → %s:%d", kiss.tcp_host, kiss.tcp_port)
    else:
        LOG.info("serve: no KISS TNC configured — RF disabled")

    if kiss_link and cfg.beacon.enabled:
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
    if kiss_link:
        await kiss_link.stop()
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
    elif args.command == "run-forward":
        asyncio.run(_cmd_run_forward(args.config))
    elif args.command == "run-retention":
        asyncio.run(_cmd_run_retention(args.config))
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
