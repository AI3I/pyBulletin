from __future__ import annotations

import logging
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

AUTHFAIL_LOG_PATH = "/var/log/pybulletin/authfail.log"

# fail2ban filter matches this line format:
#   <timestamp> pybulletin AUTHFAIL channel=<ch> ip=<ip> call=<call> reason=<reason>
_LINE_FMT = (
    "{ts} pybulletin AUTHFAIL "
    "channel={channel} ip={ip} call={call} reason={reason}\n"
)


def log_auth_failure(
    call: str,
    ip: str,
    channel: str,
    reason: str,
    *,
    log_path: str = AUTHFAIL_LOG_PATH,
) -> None:
    """Append a structured auth-failure line readable by fail2ban."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    line = _LINE_FMT.format(
        ts=ts,
        channel=channel,
        ip=ip or "-",
        call=(call or "-").upper(),
        reason=reason,
    )
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        LOG.warning("auth_logging: could not write to %s: %s", log_path, exc)

    LOG.warning(
        "AUTHFAIL channel=%s ip=%s call=%s reason=%s",
        channel, ip or "-", (call or "-").upper(), reason,
    )
