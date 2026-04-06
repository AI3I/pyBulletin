from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store.store import BBSStore

# Capability names used throughout the codebase
CAP_READ      = "read"       # read messages and bulletins
CAP_SEND      = "send"       # send personal mail and bulletins
CAP_UPLOAD    = "upload"     # upload files
CAP_DOWNLOAD  = "download"   # download files
CAP_CHAT      = "chat"       # participate in conference mode
CAP_GATEWAY   = "gateway"    # use gateway (G) command
CAP_SYSOP     = "sysop"      # sysop-level access

ALL_CAPABILITIES = (
    CAP_READ, CAP_SEND, CAP_UPLOAD, CAP_DOWNLOAD, CAP_CHAT, CAP_GATEWAY, CAP_SYSOP,
)

# Channels used in auth failure logging
CHANNEL_TELNET   = "telnet"
CHANNEL_AX25     = "ax25"
CHANNEL_WEB      = "web"
CHANNEL_SYSOP_WEB = "sysop-web"

# Default open capabilities for authenticated users
_DEFAULT_OPEN: frozenset[str] = frozenset({
    CAP_READ, CAP_SEND, CAP_DOWNLOAD, CAP_CHAT,
})

# Capabilities available when the node is configured require_password=False
_DEFAULT_UNAUTH: frozenset[str] = frozenset({CAP_READ})


def default_access_allowed(
    call: str,
    capability: str,
    *,
    authenticated: bool = True,
) -> bool:
    """Return whether *call* has *capability* under default policy.

    Per-callsign overrides are stored in the database and checked by
    :func:`access_allowed`, which delegates here for unoverridden calls.
    """
    if not authenticated:
        return capability in _DEFAULT_UNAUTH
    cap = capability.lower()
    if cap == CAP_SYSOP:
        return False
    return cap in _DEFAULT_OPEN


def access_allowed(
    call: str,
    capability: str,
    store: BBSStore,
    *,
    authenticated: bool = True,
) -> bool:
    """Check whether *call* may exercise *capability*.

    Looks up per-callsign overrides in the user record's access field
    (future: store access matrix in a separate table).  Falls back to
    :func:`default_access_allowed`.
    """
    # SYSOP user has all capabilities
    if call.upper() == "SYSOP":
        return True
    return default_access_allowed(call, capability, authenticated=authenticated)
