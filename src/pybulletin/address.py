from __future__ import annotations

import re
from dataclasses import dataclass


# Matches "CALL@BBS.STATE.REGION.COUNTRY.CONT" or "CALL" or "CALL@BBS"
_ADDR_RE = re.compile(
    r"^(?P<call>[A-Z0-9][-A-Z0-9/]*)(?:@(?P<bbs>[A-Z0-9][-A-Z0-9.]*)?)?$",
    re.IGNORECASE,
)

# Well-known top-level routing areas
WW   = "WW"
NA   = "NA"    # North America
SA   = "SA"    # South America
EU   = "EU"    # Europe
AF   = "AF"    # Africa
AS   = "AS"    # Asia
OC   = "OC"    # Oceania
AN   = "AN"    # Antarctica

CONTINENTS = {WW, NA, SA, EU, AF, AS, OC, AN}


@dataclass(slots=True)
class BBSAddress:
    """Parsed representation of a BBS routing address.

    Examples::

        TO@W3BBS.PA.USA.NOAM     → call="TO", bbs="W3BBS", hierarchy=["PA","USA","NOAM"]
        WW                        → call="WW", bbs="", hierarchy=["WW"]
        CALL@W3BBS                → call="CALL", bbs="W3BBS", hierarchy=[]
    """
    call: str
    bbs: str
    # Components of the BBS address beyond the node call, e.g. ["PA","USA","NOAM"]
    hierarchy: list[str]

    @property
    def is_bulletin(self) -> bool:
        """True if the address looks like a bulletin routing key (all-caps, no digits)."""
        return bool(re.match(r"^[A-Z]{2,6}$", self.call))

    @property
    def routing_key(self) -> str:
        """The most-specific routing label for this address."""
        if self.hierarchy:
            return self.hierarchy[0]
        if self.bbs:
            return self.bbs.upper()
        return self.call.upper()

    def __str__(self) -> str:
        if self.bbs:
            suffix = ".".join(self.hierarchy)
            at_part = f"{self.bbs}.{suffix}" if suffix else self.bbs
            return f"{self.call.upper()}@{at_part.upper()}"
        return self.call.upper()


def parse_address(raw: str) -> BBSAddress:
    """Parse a BBS address string into a :class:`BBSAddress`.

    Handles::

        CALL
        CALL@BBS
        CALL@BBS.STATE.REGION.COUNTRY.CONT
        WW  (bulletin routing key)
    """
    raw = raw.strip().upper()
    m = _ADDR_RE.match(raw)
    if not m:
        return BBSAddress(call=raw, bbs="", hierarchy=[])

    call = m.group("call") or ""
    bbs_part = m.group("bbs") or ""

    if not bbs_part:
        return BBSAddress(call=call, bbs="", hierarchy=[])

    # Split BBS.STATE.REGION.COUNTRY.CONT
    parts = bbs_part.split(".")
    bbs = parts[0]
    hierarchy = parts[1:] if len(parts) > 1 else []

    return BBSAddress(call=call, bbs=bbs, hierarchy=hierarchy)


def is_local(address: BBSAddress, node_call: str) -> bool:
    """Return True if *address* is destined for *node_call* (local delivery)."""
    node = node_call.upper().split("-")[0]
    if not address.bbs:
        return False
    return address.bbs.upper().split("-")[0] == node


def matches_hierarchy(address: BBSAddress, local_hierarchy: list[str]) -> bool:
    """Return True if *address* falls within *local_hierarchy*.

    A bulletin addressed to "WW" matches any node.
    A bulletin addressed to "NA" matches nodes whose hierarchy contains "NA".

    Args:
        address:         The destination address being evaluated.
        local_hierarchy: This node's hierarchy list, e.g. ["WW","NA","US","US-PA"].
    """
    key = address.call.upper() if address.is_bulletin else ""
    if not key and address.hierarchy:
        key = address.hierarchy[-1].upper()  # most-specific component

    if not key:
        return False

    if key == WW:
        return True

    return key in (h.upper() for h in local_hierarchy)


def routing_path(address: BBSAddress, local_hierarchy: list[str]) -> list[str]:
    """Return the sequence of hops from this node toward *address*.

    Used by the forwarding engine to decide which neighbor to hand a message to.
    Returns an empty list if the address is local or unreachable.
    """
    key = address.call.upper() if address.is_bulletin else ""
    if not key:
        return []
    if key == WW:
        return list(local_hierarchy)

    try:
        idx = [h.upper() for h in local_hierarchy].index(key)
        return [local_hierarchy[i] for i in range(idx, len(local_hierarchy))]
    except ValueError:
        return []
