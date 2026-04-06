"""FBB System Identification (SID) string parsing and generation.

The SID is the first thing exchanged when two FBB nodes connect.
Format::

    [CALL-SSID-SOFTWARE-FLAGS$]

Examples::

    [W3BBS-5-FBB5.15-B2FHIM$]          FBB (F6FBB / LinFBB)
    [AI3I-1-pyBulletin0.1-B2FHM$]      pyBulletin
    [KD8JQ-1-BPQ6.0.22-B2FHM$]         BPQ32 (G8BPQ)
    [W2TKE-1-JNOS2.0m-B2FHM$]          JNOS
    [VE7VV-1-Airmail3.4.062-B2FHM$]    Winlink / Airmail
    [KA1BBS-WL2K4.1-B2FHM$]            Winlink 2000

Capability flags (relevant subset):
    B — supports B2 binary forwarding
    2 — supports B2F enhanced binary (implies B)
    F — FBB-compatible forwarding
    H — hierarchical bulletin addressing
    I — Internet News Node
    M — message notification
    $ — mandatory end marker (not really a flag)

We advertise: B2FHM  (B2F binary, FBB compat, hierarchical, message notify)

BPQ / JNOS / Kantronics interoperability notes
-----------------------------------------------
BPQ32 (G8BPQ) and JNOS follow the standard SID format and parse cleanly.
Both advertise ``B2FHM$`` so full B2F forwarding works.

Kantronics KPC-3/9612 built-in PBBS uses a non-standard SID like
``[KA1BBS-KPC-3v8.3-BF$]`` (dashes in the software field break the
FBB field grammar).  When ``parse()`` returns ``None``, the session
falls back to B1 forwarding, which the KPC PBBS also supports.

Winlink/Airmail nodes typically advertise ``B2FHM$`` and interoperate
fully over B2F.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import __version__

# Flags we advertise in our SID
_OUR_FLAGS = "B2FHM"

# Pattern: [CALL(-SSID)?-SOFTWARE-FLAGS$]
# The regex uses backtracking so that the optional -SSID doesn't greedily
# consume the dash that belongs to the software-string separator.
_SID_RE = re.compile(
    r"\[([A-Z0-9]+-?\d*)"      # call (+ optional SSID like -1)
    r"(?:-(\d+))?"             # optional standalone SSID digit  (rarely used separately)
    r"-([^-\$\]]*)"            # software string (no dashes, $, or ])
    r"-([A-Z0-9\$]*\$)"        # flags (must end with $)
    r"\]"
)

# Known software families — matched against the raw SID string for logging
# when strict parse fails.  Ordered most-specific first.
_KNOWN_SOFTWARE: list[tuple[str, str]] = [
    ("BPQ",        "BPQ32 (G8BPQ)"),
    ("JNOS",       "JNOS"),
    ("Airmail",    "Winlink/Airmail"),
    ("WL2K",       "Winlink 2000"),
    ("FBB",        "LinFBB (F6FBB)"),
    ("KPC",        "Kantronics KPC"),
    ("TNOS",       "TNOS"),
    ("MSYS",       "MSYS"),
    ("F6FBB",      "LinFBB (F6FBB)"),
    ("pyBulletin", "pyBulletin"),
]


@dataclass
class SID:
    call:       str
    ssid:       int
    software:   str
    flags:      str       # raw flag string (including $)

    @property
    def supports_b2f(self) -> bool:
        """True if the remote supports B2F binary forwarding."""
        return "B" in self.flags or "2" in self.flags

    @property
    def supports_b1(self) -> bool:
        """True if the remote supports FBB-compatible (B1/FA) forwarding."""
        return "F" in self.flags

    @property
    def supports_hierarchical(self) -> bool:
        return "H" in self.flags

    @property
    def software_family(self) -> str:
        """Human-readable software family name for logging."""
        sw_upper = self.software.upper()
        for key, label in _KNOWN_SOFTWARE:
            if key.upper() in sw_upper:
                return label
        return self.software or "unknown"

    def __str__(self) -> str:
        ssid_part = f"-{self.ssid}" if self.ssid else ""
        return f"[{self.call}{ssid_part}-{self.software}-{self.flags}]"


def parse(text: str) -> SID | None:
    """Parse a SID string, returning None if it doesn't match."""
    m = _SID_RE.search(text)
    if not m:
        return None

    raw_call = m.group(1)   # e.g. "W3BBS-1" or just "W3BBS"
    if "-" in raw_call:
        call, _, ssid_str = raw_call.rpartition("-")
        try:
            ssid = int(ssid_str)
        except ValueError:
            call, ssid = raw_call, 0
    else:
        call, ssid = raw_call, 0

    # If standalone SSID group matched, prefer it
    if m.group(2):
        try:
            ssid = int(m.group(2))
        except ValueError:
            pass

    return SID(
        call=call.upper(),
        ssid=ssid,
        software=m.group(3),
        flags=m.group(4),
    )


def detect_software(raw: str) -> str:
    """Return a human-readable software name from a raw (unparseable) SID.

    Used for logging when ``parse()`` returns ``None`` so the operator
    sees something meaningful rather than just the raw string.
    """
    for key, label in _KNOWN_SOFTWARE:
        if key.lower() in raw.lower():
            return label
    # Last resort: return the inner bracket content, trimmed
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        return inner[1:-1][:40]
    return inner[:40]


def generate(node_call: str) -> str:
    """Generate our SID string for the given node callsign."""
    if "-" in node_call:
        call, _, ssid_str = node_call.upper().partition("-")
        try:
            ssid = int(ssid_str)
        except ValueError:
            call, ssid = node_call.upper(), 0
    else:
        call, ssid = node_call.upper(), 0

    ssid_part = f"-{ssid}" if ssid else ""
    sw = f"pyBulletin{__version__}"
    return f"[{call}{ssid_part}-{sw}-{_OUR_FLAGS}$]"
