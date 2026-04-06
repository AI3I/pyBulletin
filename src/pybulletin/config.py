from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class NodeConfig:
    node_call: str = "N0BBS-1"
    node_alias: str = "N0BBS"
    owner_name: str = "BBS Sysop"
    qth: str = "Unknown"
    node_locator: str = ""
    motd: str = "Welcome to pyBulletin"
    branding_name: str = "pyBulletin"
    welcome_title: str = "Welcome"
    welcome_body: str = ""
    login_tip: str = "Type H for help, B to disconnect."
    show_status_after_login: bool = True
    require_password: bool = True
    support_contact: str = ""
    website_url: str = ""
    # Hierarchy position for bulletin routing, most-specific last
    # e.g. ["WW", "NA", "US", "US-PA"]
    hierarchy: list[str] = field(default_factory=lambda: ["WW"])


@dataclass(slots=True)
class TelnetConfig:
    host: str = "0.0.0.0"
    port: int = 6300
    # Additional listen ports (e.g. a raw TCP port alongside telnet)
    ports: tuple[int, ...] = ()
    max_clients: int = 50
    idle_timeout_seconds: int = 1800
    max_line_length: int = 256


@dataclass(slots=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(slots=True)
class PublicWebConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8081
    static_dir: str = ""


@dataclass(slots=True)
class StoreConfig:
    sqlite_path: str = "./data/pybulletin.db"
    files_path: str = "./data/files"


@dataclass(slots=True)
class RetentionConfig:
    personal_mail_days: int = 30
    bulletin_days: int = 14
    nts_days: int = 7
    # How long to keep killed messages before purging
    killed_days: int = 1


@dataclass(slots=True)
class KissConfig:
    # Serial KISS TNC
    device: str = ""
    baud: int = 9600
    # Maximum packet length in bytes (PACLEN)
    paclen: int = 236
    # KISS over TCP (Dire Wolf, soundmodem, etc.)
    tcp_host: str = ""
    tcp_port: int = 8001
    # Commands sent to a hardware TNC before switching to KISS mode.
    # Kantronics KPC-3/9612: ["INTFACE KISS", "RESET"]
    # TAPR TNC-2 / MFJ:      ["KISS ON"]
    # Leave empty if TNC is already in KISS mode (Dire Wolf, soundmodem).
    init_cmds: list[str] = field(default_factory=list)
    # Milliseconds to wait between init commands (and after the last one)
    init_delay_ms: int = 500


@dataclass(slots=True)
class BeaconConfig:
    enabled: bool = False
    # {node_call} and {version} are substituted at runtime
    text: str = "pyBulletin BBS {node_call} - {version}"
    interval_seconds: int = 600
    # Digipeater path, e.g. "WIDE1-1" or "" for no digipeat
    path: str = ""


@dataclass(slots=True)
class RateLimitConfig:
    max_connections_per_ip: int = 5
    max_messages_per_session_per_hour: int = 20
    # Maximum message body size in bytes
    max_message_body_bytes: int = 32768


@dataclass(slots=True)
class PactorConfig:
    """PACTOR TNC transport configuration.

    PACTOR-I/II/III/IV are proprietary SCS protocols and require SCS-licensed
    hardware (PTC-IIusb, PTC-IIIusb, PTC-IVex, Dragon, etc.).

    pyBulletin connects to the TNC via the WA8DED / HOST mode serial interface
    that SCS modems support.  Set ``device`` to the serial port attached to
    your SCS modem.

    Supported devices (tested or documented):
      SCS PTC-IIusb, PTC-IIIusb, PTC-IVex, Dragon  — HOST mode via USB/serial
    """
    enabled: bool = False
    device: str = ""
    baud: int = 115200
    # Maximum frame size in bytes (PACTOR default is 250)
    paclen: int = 250


@dataclass(slots=True)
class ForwardNeighborConfig:
    call: str = ""
    # TCP address as "host:port", or AX.25 callsign for RF connect
    address: str = ""
    # Forwarding protocol: "b1" or "b2"
    protocol: str = "b2"
    # Cron expression for when to initiate forwarding (UTC)
    schedule: str = "0 */2 * * *"
    # Bulletin address prefixes accepted from/sent to this neighbor
    categories: list[str] = field(default_factory=lambda: ["WW"])
    bin_mode: bool = True
    enabled: bool = True


@dataclass(slots=True)
class ForwardConfig:
    enabled: bool = True
    neighbors: list[ForwardNeighborConfig] = field(default_factory=list)


@dataclass(slots=True)
class AppConfig:
    node: NodeConfig = field(default_factory=NodeConfig)
    telnet: TelnetConfig = field(default_factory=TelnetConfig)
    web: WebConfig = field(default_factory=WebConfig)
    public_web: PublicWebConfig = field(default_factory=PublicWebConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    kiss: KissConfig = field(default_factory=KissConfig)
    beacon: BeaconConfig = field(default_factory=BeaconConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    forward: ForwardConfig = field(default_factory=ForwardConfig)
    pactor: PactorConfig = field(default_factory=PactorConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (non-destructive copy)."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _build_node(d: dict) -> NodeConfig:
    c = NodeConfig()
    for k in ("node_call", "node_alias", "owner_name", "qth", "node_locator",
              "motd", "branding_name", "welcome_title", "welcome_body",
              "login_tip", "support_contact", "website_url"):
        if k in d:
            object.__setattr__(c, k, str(d[k]))
    for k in ("show_status_after_login", "require_password"):
        if k in d:
            object.__setattr__(c, k, bool(d[k]))
    if "hierarchy" in d:
        object.__setattr__(c, "hierarchy", [str(x) for x in d["hierarchy"]])
    return c


def _build_telnet(d: dict) -> TelnetConfig:
    c = TelnetConfig()
    if "host" in d:
        object.__setattr__(c, "host", str(d["host"]))
    if "port" in d:
        object.__setattr__(c, "port", int(d["port"]))
    if "ports" in d:
        object.__setattr__(c, "ports", tuple(int(p) for p in d["ports"]))
    if "max_clients" in d:
        object.__setattr__(c, "max_clients", int(d["max_clients"]))
    if "idle_timeout_seconds" in d:
        object.__setattr__(c, "idle_timeout_seconds", int(d["idle_timeout_seconds"]))
    if "max_line_length" in d:
        object.__setattr__(c, "max_line_length", int(d["max_line_length"]))
    return c


def _build_web(d: dict) -> WebConfig:
    c = WebConfig()
    if "host" in d:
        object.__setattr__(c, "host", str(d["host"]))
    if "port" in d:
        object.__setattr__(c, "port", int(d["port"]))
    return c


def _build_public_web(d: dict) -> PublicWebConfig:
    c = PublicWebConfig()
    if "enabled" in d:
        object.__setattr__(c, "enabled", bool(d["enabled"]))
    if "host" in d:
        object.__setattr__(c, "host", str(d["host"]))
    if "port" in d:
        object.__setattr__(c, "port", int(d["port"]))
    if "static_dir" in d:
        object.__setattr__(c, "static_dir", str(d["static_dir"]))
    return c


def _build_store(d: dict) -> StoreConfig:
    c = StoreConfig()
    if "sqlite_path" in d:
        object.__setattr__(c, "sqlite_path", str(d["sqlite_path"]))
    if "files_path" in d:
        object.__setattr__(c, "files_path", str(d["files_path"]))
    return c


def _build_retention(d: dict) -> RetentionConfig:
    c = RetentionConfig()
    for k in ("personal_mail_days", "bulletin_days", "nts_days", "killed_days"):
        if k in d:
            object.__setattr__(c, k, int(d[k]))
    return c


def _build_kiss(d: dict) -> KissConfig:
    c = KissConfig()
    if "device" in d:
        object.__setattr__(c, "device", str(d["device"]))
    if "baud" in d:
        object.__setattr__(c, "baud", int(d["baud"]))
    if "paclen" in d:
        object.__setattr__(c, "paclen", int(d["paclen"]))
    if "tcp_host" in d:
        object.__setattr__(c, "tcp_host", str(d["tcp_host"]))
    if "tcp_port" in d:
        object.__setattr__(c, "tcp_port", int(d["tcp_port"]))
    if "init_cmds" in d:
        object.__setattr__(c, "init_cmds", [str(x) for x in d["init_cmds"]])
    if "init_delay_ms" in d:
        object.__setattr__(c, "init_delay_ms", int(d["init_delay_ms"]))
    return c


def _build_beacon(d: dict) -> BeaconConfig:
    c = BeaconConfig()
    if "enabled" in d:
        object.__setattr__(c, "enabled", bool(d["enabled"]))
    if "text" in d:
        object.__setattr__(c, "text", str(d["text"]))
    if "interval_seconds" in d:
        object.__setattr__(c, "interval_seconds", int(d["interval_seconds"]))
    if "path" in d:
        object.__setattr__(c, "path", str(d["path"]))
    return c


def _build_rate_limit(d: dict) -> RateLimitConfig:
    c = RateLimitConfig()
    for k in ("max_connections_per_ip", "max_messages_per_session_per_hour",
              "max_message_body_bytes"):
        if k in d:
            object.__setattr__(c, k, int(d[k]))
    return c


def _build_pactor(d: dict) -> PactorConfig:
    c = PactorConfig()
    if "enabled" in d:
        object.__setattr__(c, "enabled", bool(d["enabled"]))
    if "device" in d:
        object.__setattr__(c, "device", str(d["device"]))
    if "baud" in d:
        object.__setattr__(c, "baud", int(d["baud"]))
    if "paclen" in d:
        object.__setattr__(c, "paclen", int(d["paclen"]))
    return c


def _build_neighbor(d: dict) -> ForwardNeighborConfig:
    c = ForwardNeighborConfig()
    if "call" in d:
        object.__setattr__(c, "call", str(d["call"]).upper())
    if "address" in d:
        object.__setattr__(c, "address", str(d["address"]))
    if "protocol" in d:
        object.__setattr__(c, "protocol", str(d["protocol"]).lower())
    if "schedule" in d:
        object.__setattr__(c, "schedule", str(d["schedule"]))
    if "categories" in d:
        object.__setattr__(c, "categories", [str(x).upper() for x in d["categories"]])
    if "bin_mode" in d:
        object.__setattr__(c, "bin_mode", bool(d["bin_mode"]))
    if "enabled" in d:
        object.__setattr__(c, "enabled", bool(d["enabled"]))
    return c


def _build_forward(d: dict) -> ForwardConfig:
    c = ForwardConfig()
    if "enabled" in d:
        object.__setattr__(c, "enabled", bool(d["enabled"]))
    neighbors = [_build_neighbor(n) for n in d.get("neighbor", [])]
    object.__setattr__(c, "neighbors", neighbors)
    return c


def _build_config(data: dict) -> AppConfig:
    return AppConfig(
        node=_build_node(data.get("node", {})),
        telnet=_build_telnet(data.get("telnet", {})),
        web=_build_web(data.get("web", {})),
        public_web=_build_public_web(data.get("public_web", {})),
        store=_build_store(data.get("store", {})),
        retention=_build_retention(data.get("retention", {})),
        kiss=_build_kiss(data.get("kiss", {})),
        beacon=_build_beacon(data.get("beacon", {})),
        rate_limit=_build_rate_limit(data.get("rate_limit", {})),
        forward=_build_forward(data.get("forward", {})),
        pactor=_build_pactor(data.get("pactor", {})),
    )


def load_config(path: str) -> AppConfig:
    """Load config from *path*, merging an adjacent *.local.toml* if present."""
    p = Path(path)
    with p.open("rb") as f:
        data = tomllib.load(f)

    local = p.with_suffix("").with_suffix(".local.toml")
    if local.exists():
        with local.open("rb") as f:
            data = _deep_merge(data, tomllib.load(f))

    return _build_config(data)


def save_config(config: AppConfig, path: str) -> None:
    """Persist runtime-editable fields to the local override file."""
    p = Path(path)
    local = p.with_suffix("").with_suffix(".local.toml")

    try:
        if sys.version_info >= (3, 11):
            import tomllib as _tl
        else:
            import tomli as _tl  # type: ignore[no-redef]
        with local.open("rb") as f:
            existing: dict = _tl.load(f)
    except FileNotFoundError:
        existing = {}

    # Node presentation fields
    existing.setdefault("node", {})
    for k in ("node_call", "node_alias", "owner_name", "qth", "node_locator",
              "motd", "branding_name", "welcome_title", "welcome_body",
              "login_tip", "support_contact", "website_url"):
        existing["node"][k] = getattr(config.node, k)

    # Write scalar sections (everything except forward.neighbor which is AoT)
    fwd_scalars = {"enabled": config.forward.enabled}
    non_fwd = {k: v for k, v in existing.items() if k != "forward"}

    lines = ["# pyBulletin local config overrides — managed by sysop console\n"]
    lines += _dict_to_toml(non_fwd)

    # Forward section header + enabled flag
    lines.append("\n[forward]\n")
    lines.append(f"enabled = {'true' if fwd_scalars['enabled'] else 'false'}\n")

    # Forward neighbors as TOML array of tables ([[forward.neighbor]])
    for n in config.forward.neighbors:
        lines.append("\n[[forward.neighbor]]\n")
        lines.append(f'call       = "{n.call}"\n')
        escaped_addr = n.address.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'address    = "{escaped_addr}"\n')
        lines.append(f'protocol   = "{n.protocol}"\n')
        lines.append(f'schedule   = "{n.schedule}"\n')
        cats = ", ".join(f'"{c}"' for c in n.categories)
        lines.append(f'categories = [{cats}]\n')
        lines.append(f'bin_mode   = {"true" if n.bin_mode else "false"}\n')
        lines.append(f'enabled    = {"true" if n.enabled else "false"}\n')

    local.write_text("".join(lines), encoding="utf-8")


def _dict_to_toml(d: dict, prefix: str = "") -> list[str]:
    """Minimal TOML serialiser for string/bool/int/list scalars (no arrays of tables)."""
    lines: list[str] = []
    scalars: list[tuple[str, object]] = []
    tables: list[tuple[str, dict]] = []

    for k, v in d.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            scalars.append((k, v))

    for k, v in scalars:
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}\n")
        elif isinstance(v, int):
            lines.append(f"{k} = {v}\n")
        elif isinstance(v, list):
            items = ", ".join(f'"{x}"' if isinstance(x, str) else str(x) for x in v)
            lines.append(f"{k} = [{items}]\n")
        else:
            escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{escaped}"\n')

    for k, v in tables:
        section = f"{prefix}.{k}" if prefix else k
        lines.append(f"\n[{section}]\n")
        lines += _dict_to_toml(v, section)

    return lines
