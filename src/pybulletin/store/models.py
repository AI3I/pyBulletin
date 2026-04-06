from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

# msg_type values
MSG_PRIVATE  = "P"   # personal mail
MSG_BULLETIN = "B"   # bulletin
MSG_NTS      = "T"   # NTS traffic

# status values
STATUS_NEW        = "N"   # unread, not yet forwarded
STATUS_READ       = "Y"   # read by recipient
STATUS_KILLED     = "K"   # deleted
STATUS_HELD       = "H"   # held by sysop
STATUS_FORWARDED  = "F"   # forwarded to another BBS
STATUS_FWD_BULL   = "$"   # forwarded bulletin (received via forwarding)

# privilege levels (mirrors pyCluster)
PRIV_NONE  = ""       # unauthenticated / guest
PRIV_USER  = "user"   # authenticated user
PRIV_SYSOP = "sysop"  # sysop


@dataclass
class Message:
    # Sequential message number assigned by this BBS (globally unique on this node)
    id: int = 0
    # Bulletin ID — globally unique across the network, used for duplicate detection
    # Format: <callsign><yymmdd><hhmmss> or sysop-assigned string, max 12 chars
    bid: str = ""
    msg_type: str = MSG_PRIVATE
    status: str = STATUS_NEW
    from_call: str = ""
    to_call: str = ""
    # Destination BBS address, e.g. "W3BBS" or "W3BBS.PA.USA.NOAM"
    at_bbs: str = ""
    subject: str = ""
    body: str = ""
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime | None = None
    # Computed from body at insert time
    size: int = 0
    # Space-separated list of BBS callsigns this message has passed through
    forward_path: str = ""
    # Callsign that read it (personal mail only)
    read_by: str = ""
    read_at: datetime | None = None
    # Sysop edit tracking
    edited_by: str = ""
    edited_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.size and self.body:
            self.size = len(self.body.encode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

@dataclass
class User:
    call: str = ""
    # Display name (free-form, mirrors pyCluster user_registry.display_name)
    display_name: str = ""
    # Privilege level: "" | "user" | "sysop"
    privilege: str = PRIV_NONE
    # Contact info
    email: str = ""
    # Mailing address (free-form)
    address: str = ""
    # BBS-specific fields retained from FBB / traditional PBBS
    home_bbs: str = ""
    # QRA/Maidenhead locator, e.g. "FN20"
    locator: str = ""
    city: str = ""
    zip_code: str = ""
    password_hash: str = ""
    # Message number the user last read up to; L shows messages after this
    msg_base: int = 0
    page_length: int = 24
    expert_mode: bool = False
    language: str = "EN"
    # Login tracking
    last_login_at: datetime | None = None
    last_login_peer: str = ""
    last_seen: datetime = field(default_factory=_now)
    created_at: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# User preferences (key/value per call, mirrors pyCluster user_prefs)
# ---------------------------------------------------------------------------

@dataclass
class UserPref:
    call: str = ""
    key: str = ""
    value: str = ""


# ---------------------------------------------------------------------------
# MFA challenge (mirrors pyCluster mfa_challenges)
# ---------------------------------------------------------------------------

@dataclass
class MfaChallenge:
    call: str = ""
    code: str = ""
    channel: str = ""
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime = field(default_factory=_now)
    used: bool = False


# ---------------------------------------------------------------------------
# White Pages entry
# ---------------------------------------------------------------------------

@dataclass
class WPEntry:
    """Propagated callsign → home-BBS mapping (FBB White Pages database)."""
    call: str = ""
    home_bbs: str = ""
    name: str = ""
    updated_at: datetime = field(default_factory=_now)
    # The BBS that sent us this record
    source_bbs: str = ""


# ---------------------------------------------------------------------------
# Forward neighbor
# ---------------------------------------------------------------------------

@dataclass
class ForwardNeighbor:
    """Runtime state for a configured forwarding neighbor."""
    call: str = ""
    last_connect_at: datetime | None = None
    last_success_at: datetime | None = None
    msgs_sent: int = 0
    msgs_received: int = 0
    # Whether this neighbor is currently in an active session
    session_active: bool = False
    enabled: bool = True


# ---------------------------------------------------------------------------
# File entry
# ---------------------------------------------------------------------------

@dataclass
class FileEntry:
    filename: str = ""
    description: str = ""
    # File area / subdirectory name
    area: str = ""
    owner: str = ""
    size: int = 0
    created_at: datetime = field(default_factory=_now)
    downloads: int = 0
