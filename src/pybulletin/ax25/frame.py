"""AX.25 frame encode/decode (v2.2, modulo-8 windowing).

Supports:
  - Address field: up to 8 addresses (dest + src + 6 repeaters), SSID 0-15
  - UI, I, S (RR/RNR/REJ), and key U frames (SABM, UA, DISC, DM)
  - PID byte for UI and I frames
  - Command/Response bit handling
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


# ---------------------------------------------------------------------------
# PID values
# ---------------------------------------------------------------------------
PID_NO_L3  = 0xF0   # No Layer 3 (plain text / PBBS)
PID_NETROM = 0xCF
PID_IP     = 0xCC
PID_ARP    = 0xCD

# ---------------------------------------------------------------------------
# Control byte constants (U frames, P/F bit set to 0 unless noted)
# ---------------------------------------------------------------------------
_CTRL_UI    = 0x03   # Unnumbered Information        (P/F=0)
_CTRL_SABM  = 0x2F   # Set Asynchronous Balanced Mode (P=1)
_CTRL_DISC  = 0x43   # Disconnect                     (P=1)
_CTRL_UA    = 0x63   # Unnumbered Acknowledgment      (F=1)
_CTRL_DM    = 0x0F   # Disconnected Mode              (F=1)
_CTRL_FRMR  = 0x87   # Frame Reject

# S-frame type selector (bits 3-2 of control byte, after stripping NR and P/F)
_STYPE_RR   = 0x00   # Receive Ready
_STYPE_RNR  = 0x01   # Receive Not Ready
_STYPE_REJ  = 0x02   # Reject

# Masks
_U_MASK   = 0b11101111   # mask out P/F bit from U frames
_PF_BIT   = 0x10         # Poll/Final bit position in U frames


class FrameType(Enum):
    I    = auto()   # Information
    RR   = auto()   # Receive Ready
    RNR  = auto()   # Receive Not Ready
    REJ  = auto()   # Reject
    UI   = auto()   # Unnumbered Information
    SABM = auto()   # Set ABM (connect request)
    UA   = auto()   # Unnumbered Ack
    DISC = auto()   # Disconnect
    DM   = auto()   # Disconnected Mode
    FRMR = auto()   # Frame Reject
    UNKNOWN = auto()


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------

@dataclass
class AX25Address:
    callsign: str   # e.g. "W3BBS" — no SSID suffix
    ssid: int = 0   # 0-15
    # command/response (dest) or has-been-repeated (repeater) bit
    ch: bool = False
    # Set on the last address in the address field
    end: bool = False

    @classmethod
    def parse(cls, s: str) -> AX25Address:
        """Parse 'CALL' or 'CALL-N' into an AX25Address."""
        s = s.strip().upper()
        if "-" in s:
            call, ssid_str = s.rsplit("-", 1)
            try:
                ssid = int(ssid_str)
            except ValueError:
                ssid = 0
        else:
            call = s
            ssid = 0
        return cls(callsign=call, ssid=ssid)

    @classmethod
    def decode(cls, data: bytes) -> AX25Address:
        """Decode a 7-byte AX.25 address field."""
        if len(data) < 7:
            raise ValueError(f"Address field too short: {len(data)} bytes")
        # Bytes 0-5: callsign characters, each shifted left 1 bit
        callsign = "".join(chr(data[i] >> 1) for i in range(6)).rstrip()
        ssid_byte = data[6]
        ssid = (ssid_byte >> 1) & 0x0F
        ch   = bool(ssid_byte & 0x80)
        end  = bool(ssid_byte & 0x01)
        return cls(callsign=callsign, ssid=ssid, ch=ch, end=end)

    def encode(self) -> bytes:
        """Encode to 7 bytes. The *end* flag should be set by the caller."""
        padded = self.callsign.upper().ljust(6)[:6]
        addr = bytearray(b << 1 for b in padded.encode("ascii", errors="replace"))
        ssid_byte = 0x60  # reserved bits always 1
        ssid_byte |= (int(self.ch) << 7)
        ssid_byte |= ((self.ssid & 0x0F) << 1)
        ssid_byte |= int(self.end)
        addr.append(ssid_byte)
        return bytes(addr)

    def __str__(self) -> str:
        if self.ssid:
            return f"{self.callsign}-{self.ssid}"
        return self.callsign

    def matches(self, other: AX25Address) -> bool:
        """Return True if callsign and SSID match (ignores ch/end bits)."""
        return (self.callsign.upper() == other.callsign.upper()
                and self.ssid == other.ssid)


# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------

@dataclass
class AX25Frame:
    dest:      AX25Address
    src:       AX25Address
    repeaters: list[AX25Address] = field(default_factory=list)
    control:   int = _CTRL_UI
    pid:       int | None = None   # present for I and UI frames
    info:      bytes = b""

    # ------------------------------------------------------------------
    # Type classification
    # ------------------------------------------------------------------

    @property
    def frame_type(self) -> FrameType:
        c = self.control
        if (c & 0x01) == 0:
            return FrameType.I
        if (c & 0x03) == 0x01:
            stype = (c >> 2) & 0x03
            return {0: FrameType.RR, 1: FrameType.RNR, 2: FrameType.REJ}.get(
                stype, FrameType.UNKNOWN
            )
        # U frame — mask out P/F bit and compare
        masked = c & _U_MASK
        _U_MAP = {
            _CTRL_UI   & _U_MASK: FrameType.UI,
            _CTRL_SABM & _U_MASK: FrameType.SABM,
            _CTRL_UA   & _U_MASK: FrameType.UA,
            _CTRL_DISC & _U_MASK: FrameType.DISC,
            _CTRL_DM   & _U_MASK: FrameType.DM,
            _CTRL_FRMR & _U_MASK: FrameType.FRMR,
        }
        return _U_MAP.get(masked, FrameType.UNKNOWN)

    @property
    def ns(self) -> int:
        """Send sequence number N(S) — valid for I frames only."""
        return (self.control >> 1) & 0x07

    @property
    def nr(self) -> int:
        """Receive sequence number N(R) — valid for I and S frames."""
        return (self.control >> 5) & 0x07

    @property
    def pf(self) -> bool:
        """Poll/Final bit."""
        return bool(self.control & 0x10)

    # ------------------------------------------------------------------
    # Encode/decode
    # ------------------------------------------------------------------

    @classmethod
    def decode(cls, data: bytes) -> AX25Frame:
        """Decode a raw AX.25 frame (no KISS framing)."""
        if len(data) < 15:  # 7+7+1 minimum
            raise ValueError("Frame too short")

        i = 0
        addresses: list[AX25Address] = []
        while i + 7 <= len(data):
            addr = AX25Address.decode(data[i:i + 7])
            addresses.append(addr)
            i += 7
            if addr.end:
                break

        if len(addresses) < 2:
            raise ValueError("Frame has fewer than 2 address fields")

        dest = addresses[0]
        src  = addresses[1]
        reps = addresses[2:]

        if i >= len(data):
            raise ValueError("No control byte")
        control = data[i]
        i += 1

        # Check if this is a frame type that carries PID
        ft = cls(dest=dest, src=src, repeaters=reps, control=control).frame_type
        pid  = None
        info = b""
        if ft in (FrameType.I, FrameType.UI):
            if i < len(data):
                pid = data[i]
                i += 1
            info = data[i:]
        elif ft == FrameType.FRMR:
            info = data[i:]

        return cls(
            dest=dest,
            src=src,
            repeaters=reps,
            control=control,
            pid=pid,
            info=info,
        )

    def encode(self) -> bytes:
        """Encode to raw AX.25 bytes (no KISS framing)."""
        addrs = [self.dest] + [self.src] + self.repeaters
        # Set end-of-address bit on last address
        for i, a in enumerate(addrs):
            a.end = (i == len(addrs) - 1)

        out = bytearray()
        for a in addrs:
            out += a.encode()
        out.append(self.control)
        if self.pid is not None:
            out.append(self.pid & 0xFF)
        out += self.info
        return bytes(out)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def ui(
        cls,
        dest: AX25Address,
        src: AX25Address,
        info: bytes,
        pid: int = PID_NO_L3,
        repeaters: list[AX25Address] | None = None,
    ) -> AX25Frame:
        """Unnumbered Information frame (beacon, connectionless data)."""
        return cls(
            dest=dest, src=src,
            repeaters=repeaters or [],
            control=_CTRL_UI,
            pid=pid,
            info=info,
        )

    @classmethod
    def sabm(cls, dest: AX25Address, src: AX25Address) -> AX25Frame:
        """Set Asynchronous Balanced Mode (connect request)."""
        dest.ch = True   # command frame
        src.ch  = False
        return cls(dest=dest, src=src, control=_CTRL_SABM)

    @classmethod
    def ua(cls, dest: AX25Address, src: AX25Address, final: bool = True) -> AX25Frame:
        """Unnumbered Acknowledgment."""
        dest.ch = False  # response frame
        src.ch  = True
        ctrl = _CTRL_UA if final else (_CTRL_UA & ~_PF_BIT)
        return cls(dest=dest, src=src, control=ctrl)

    @classmethod
    def disc(cls, dest: AX25Address, src: AX25Address) -> AX25Frame:
        """Disconnect."""
        dest.ch = True
        src.ch  = False
        return cls(dest=dest, src=src, control=_CTRL_DISC)

    @classmethod
    def dm(cls, dest: AX25Address, src: AX25Address, final: bool = True) -> AX25Frame:
        """Disconnected Mode."""
        dest.ch = False
        src.ch  = True
        ctrl = _CTRL_DM if final else (_CTRL_DM & ~_PF_BIT)
        return cls(dest=dest, src=src, control=ctrl)

    @classmethod
    def rr(
        cls, dest: AX25Address, src: AX25Address,
        nr: int, pf: bool = False, command: bool = False,
    ) -> AX25Frame:
        """Receive Ready (I-frame acknowledgment)."""
        dest.ch = command
        src.ch  = not command
        ctrl = ((nr & 0x07) << 5) | (int(pf) << 4) | 0x01
        return cls(dest=dest, src=src, control=ctrl)

    @classmethod
    def rnr(
        cls, dest: AX25Address, src: AX25Address,
        nr: int, pf: bool = False, command: bool = False,
    ) -> AX25Frame:
        """Receive Not Ready (flow control)."""
        dest.ch = command
        src.ch  = not command
        ctrl = ((nr & 0x07) << 5) | (int(pf) << 4) | 0x05
        return cls(dest=dest, src=src, control=ctrl)

    @classmethod
    def rej(
        cls, dest: AX25Address, src: AX25Address,
        nr: int, pf: bool = False, command: bool = False,
    ) -> AX25Frame:
        """Reject (request retransmission from N(R))."""
        dest.ch = command
        src.ch  = not command
        ctrl = ((nr & 0x07) << 5) | (int(pf) << 4) | 0x09
        return cls(dest=dest, src=src, control=ctrl)

    @classmethod
    def iframe(
        cls,
        dest: AX25Address,
        src: AX25Address,
        ns: int,
        nr: int,
        info: bytes,
        pid: int = PID_NO_L3,
        pf: bool = False,
    ) -> AX25Frame:
        """Information frame."""
        dest.ch = True   # command
        src.ch  = False
        ctrl = ((nr & 0x07) << 5) | (int(pf) << 4) | ((ns & 0x07) << 1)
        return cls(dest=dest, src=src, control=ctrl, pid=pid, info=info)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        via = ""
        if self.repeaters:
            via = " via " + ",".join(str(r) for r in self.repeaters)
        return (f"{self.dest}←{self.src}{via} "
                f"[{self.frame_type.name} ctrl=0x{self.control:02x}] "
                f"len={len(self.info)}")
