"""Choose forwarding peers from message @ addresses and peer route areas."""
from __future__ import annotations

import re
from typing import Iterable

from ..store.models import Message, MSG_BULLETIN, MSG_NTS, MSG_PRIVATE

_BBS_CALL = re.compile(r"(?=[A-Z0-9-]*[0-9])[A-Z0-9]{1,6}(?:-(?:[0-9]|1[0-5]))?\Z")


def _area_score(address: str, area: str) -> int:
    """Return a route score for a whole hierarchical-address suffix."""
    area = area.strip().upper().lstrip("@")
    if not area:
        return -1
    if area == "WW":
        return 0
    if address == area or address.endswith("." + area):
        return len(area.split(".")) * 100 + len(area)
    return -1


def matching_neighbors(message: Message, neighbors: Iterable, *, node_call: str = "") -> list:
    """Return peers eligible for one message, in configured order.

    Private and NTS traffic, plus a bulletin addressed to a named BBS, use
    one best route. Area bulletins are flooded to every matching peer. WW is
    the catch-all area. Legacy bulletins without an @ address use their To
    field as an area so existing local messages continue to forward.
    """
    address = message.at_bbs.strip().upper()
    if not address and message.msg_type in (MSG_BULLETIN, MSG_NTS):
        address = message.to_call.strip().upper()
    if not address:
        return []

    first = address.split(".", 1)[0]
    directed = message.msg_type in (MSG_PRIVATE, MSG_NTS) or (
        message.msg_type == MSG_BULLETIN and _BBS_CALL.fullmatch(first) is not None
    )
    path = set(message.forward_path.upper().split())
    if node_call and node_call.upper() in path:
        return []
    candidates = []
    for index, neighbor in enumerate(neighbors):
        call = neighbor.call.upper()
        if not neighbor.enabled or call in path or call == node_call.upper():
            continue
        direct = address == call or address.startswith(call + ".")
        score = 10_000 + len(call) if direct else max(
            (_area_score(address, area) for area in neighbor.categories),
            default=-1,
        )
        if score >= 0:
            candidates.append((score, index, neighbor))

    if not candidates:
        return []
    if directed:
        best = max(candidates, key=lambda item: (item[0], -item[1]))
        return [best[2]]
    return [item[2] for item in candidates]
