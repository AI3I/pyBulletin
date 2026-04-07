"""Pure-Python LZHUF (Okumura/Yoshizaki) compression.

Used by the FBB B2F forwarding protocol to compress message bodies.

Reference implementation: lzhuf.c by Haruhiko Okumura and Haruyasu Yoshizaki.
Algorithm: LZ77 sliding-window + adaptive Huffman coding.

Public API::

    compressed   = compress(data: bytes) -> bytes
    decompressed = decompress(data: bytes, original_size: int) -> bytes
"""
from __future__ import annotations

import struct

# ---------------------------------------------------------------------------
# Constants (must match the C reference exactly for interoperability)
# ---------------------------------------------------------------------------

N         = 4096    # sliding window size
F         = 18      # maximum match length
THRESHOLD = 2       # minimum match length to encode as reference
NIL       = N       # sentinel for binary tree

# Huffman coding parameters
N_CHAR  = 256 - THRESHOLD + F   # 269 — number of symbol codes
T       = N_CHAR * 2 - 1        # size of Huffman tree table
R       = T - 1                 # position of root
MAX_FREQ = 0x8000                # rescale when freq[R] reaches this


# ---------------------------------------------------------------------------
# Bit I/O helpers
# ---------------------------------------------------------------------------

class _BitWriter:
    __slots__ = ("_buf", "_byte", "_mask")

    def __init__(self) -> None:
        self._buf: list[int] = []
        self._byte = 0
        self._mask = 0x80

    def write_bit(self, bit: int) -> None:
        if bit:
            self._byte |= self._mask
        self._mask >>= 1
        if not self._mask:
            self._buf.append(self._byte)
            self._byte = 0
            self._mask = 0x80

    def write_bits(self, value: int, n: int) -> None:
        mask = 1 << (n - 1)
        while mask:
            self.write_bit(1 if (value & mask) else 0)
            mask >>= 1

    def flush(self) -> bytes:
        if self._mask != 0x80:
            self._buf.append(self._byte)
        return bytes(self._buf)


class _BitReader:
    __slots__ = ("_data", "_pos", "_byte", "_mask", "_eof")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos  = 0
        self._byte = 0
        self._mask = 0
        self._eof  = False

    def _next_byte(self) -> int:
        if self._pos >= len(self._data):
            self._eof = True
            return 0
        b = self._data[self._pos]
        self._pos += 1
        return b

    def read_bit(self) -> int:
        if not self._mask:
            self._byte = self._next_byte()
            self._mask = 0x80
        bit = 1 if (self._byte & self._mask) else 0
        self._mask >>= 1
        return bit

    def read_bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.read_bit()
        return value

    @property
    def eof(self) -> bool:
        return self._eof


# ---------------------------------------------------------------------------
# Adaptive Huffman tree
# ---------------------------------------------------------------------------

class _HuffmanTree:
    """Adaptive Huffman tree shared by encoder and decoder."""

    __slots__ = ("freq", "prnt", "son")

    def __init__(self) -> None:
        freq = [0] * (T + 1)
        prnt = [0] * (T + N_CHAR)
        son  = [0] * T

        # Initialise leaves
        for i in range(N_CHAR):
            freq[i] = 1
            son[i]  = i + T
            prnt[i + T] = i

        # Build initial tree bottom-up
        i = 0
        j = N_CHAR
        while j <= R:
            freq[j] = freq[i] + freq[i + 1]
            son[j]  = i
            prnt[i] = prnt[i + 1] = j
            i += 2
            j += 1
        freq[T] = 0xFFFF
        prnt[R] = 0

        self.freq = freq
        self.prnt = prnt
        self.son  = son

    def update(self, c: int) -> None:
        """Increment frequency of symbol *c* and rebalance."""
        freq = self.freq
        prnt = self.prnt
        son  = self.son

        if freq[R] == MAX_FREQ:
            self._rescale()

        c = prnt[c + T]
        while c != R:
            freq[c] += 1
            k = c + 1
            if freq[k] < freq[c]:
                # Find the node to swap with
                while freq[k + 1] < freq[c]:
                    k += 1
                # Swap c and k in the tree
                l = son[c]
                son[c]  = son[k]
                son[k]  = l
                prnt[son[c]] = c
                if son[c] < T:
                    prnt[son[c] + 1] = c
                prnt[son[k]] = k
                if son[k] < T:
                    prnt[son[k] + 1] = k
                # Swap frequencies
                freq[c], freq[k] = freq[k], freq[c]
                c = k
            c = prnt[c]

    def _rescale(self) -> None:
        freq = self.freq
        son  = self.son
        prnt = self.prnt

        # Halve all leaf frequencies
        j = 0
        for i in range(T):
            if son[i] >= T:
                freq[j] = (freq[i] + 1) >> 1
                son[j]  = son[i]
                j += 1

        # Rebuild internal nodes
        i = 0
        j = N_CHAR
        while j < T:
            k = i + 1
            f = freq[i] + freq[k]
            freq[j] = f
            k = j - 1
            while freq[k] > f:
                k -= 1
            k += 1
            # Shift entries up
            ll = (j - k) * 4  # number of ints to move (simplified)
            freq[k + 1:j + 1] = freq[k:j]
            freq[k] = f
            son[k + 1:j + 1] = son[k:j]
            son[k] = i
            i += 2
            j += 1

        # Rebuild prnt
        for i in range(T):
            k = son[i]
            prnt[k] = i
            if k < T:
                prnt[k + 1] = i

    def encode_char(self, c: int, writer: _BitWriter) -> None:
        """Write the Huffman code for symbol *c*."""
        son      = self.son
        prnt     = self.prnt
        sym_node = c          # save original symbol index
        bits: list[int] = []
        cur  = prnt[sym_node + T]
        prev = sym_node + T
        while True:
            # bit = 0 if prev is left child (son[cur]), 1 if right child
            bits.append(0 if son[cur] == prev else 1)
            if cur == R:
                break
            prev = cur
            cur  = prnt[cur]
        for b in reversed(bits):
            writer.write_bit(b)
        self.update(sym_node)

    def decode_char(self, reader: _BitReader) -> int:
        """Read bits until a leaf is reached; return the symbol."""
        son  = self.son
        node = son[R]
        while node < T:
            node = son[node + reader.read_bit()]
        node -= T
        self.update(node)
        return node


# ---------------------------------------------------------------------------
# Sliding-window string matching (binary search tree)
# ---------------------------------------------------------------------------

class _TextBuffer:
    __slots__ = ("buf", "lson", "rson", "dad",
                 "match_pos", "match_len")

    def __init__(self) -> None:
        self.buf  = bytearray(N + F - 1)
        # Binary tree for O(log N) string matching
        self.lson = [NIL] * (N + 1)
        self.rson = [NIL] * (N + 257)
        self.dad  = [NIL] * (N + 1)
        self.match_pos = 0
        self.match_len = 0

    def insert_node(self, r: int) -> None:
        buf   = self.buf
        lson  = self.lson
        rson  = self.rson
        dad   = self.dad

        cmp_result = 1
        key = r
        p   = N + 1 + buf[r]
        rson[r] = lson[r] = NIL
        self.match_len = 0

        while True:
            if cmp_result >= 0:
                if rson[p] != NIL:
                    p = rson[p]
                else:
                    rson[p] = r
                    dad[r]  = p
                    return
            else:
                if lson[p] != NIL:
                    p = lson[p]
                else:
                    lson[p] = r
                    dad[r]  = p
                    return

            i = 1
            while i < F and buf[(r + i) % N] == buf[(p + i) % N]:
                i += 1
            cmp_result = buf[(r + i) % N] - buf[(p + i) % N] if i < F else 0

            if i > self.match_len:
                self.match_pos = p
                self.match_len = i
                if i >= F:
                    break

        dad[r]     = dad[p]
        lson[r]    = lson[p]
        rson[r]    = rson[p]
        dad[lson[p]] = r
        dad[rson[p]] = r
        if rson[dad[p]] == p:
            rson[dad[p]] = r
        else:
            lson[dad[p]] = r
        dad[p] = NIL

    def delete_node(self, p: int) -> None:
        lson = self.lson
        rson = self.rson
        dad  = self.dad

        if dad[p] == NIL:
            return
        if rson[p] == NIL:
            q = lson[p]
        elif lson[p] == NIL:
            q = rson[p]
        else:
            q = lson[p]
            if rson[q] != NIL:
                while rson[q] != NIL:
                    q = rson[q]
                rson[dad[q]] = lson[q]
                dad[lson[q]] = dad[q]
                lson[q] = lson[p]
                dad[lson[p]] = q
            rson[q]       = rson[p]
            dad[rson[p]]  = q

        dad[q] = dad[p]
        if rson[dad[p]] == p:
            rson[dad[p]] = q
        else:
            lson[dad[p]] = q
        dad[p] = NIL


# ---------------------------------------------------------------------------
# Public compress / decompress
# ---------------------------------------------------------------------------

def compress(data: bytes) -> bytes:
    """Compress *data* with LZHUF.

    Returns the compressed bytes preceded by a 4-byte little-endian
    original-size header (same format as FBB B2F expects).
    """
    original_size = len(data)
    writer = _BitWriter()
    tree   = _HuffmanTree()
    tb     = _TextBuffer()

    buf  = tb.buf
    r    = N - F
    s    = 0
    last_match_len = 0
    in_pos = 0

    # Pre-fill buffer with spaces
    for i in range(N - F):
        buf[i] = ord(' ')

    # Read initial lookahead
    len_ = 0
    while len_ < F and in_pos < len(data):
        buf[r + len_] = data[in_pos]
        in_pos += 1
        len_ += 1

    for i in range(1, F + 1):
        tb.insert_node((r - i) % N)
    tb.insert_node(r)

    while len_ > 0:
        if tb.match_len > len_:
            tb.match_len = len_

        if tb.match_len <= THRESHOLD:
            tb.match_len = 1
            tree.encode_char(buf[r], writer)
        else:
            tree.encode_char(255 - THRESHOLD + tb.match_len, writer)
            writer.write_bits(
                (r - tb.match_pos - 1) % N,
                # Number of bits needed for N
                12,
            )

        last_match_len = tb.match_len
        for _ in range(last_match_len):
            tb.delete_node(s)
            if in_pos < len(data):
                c = data[in_pos]
                in_pos += 1
                buf[s] = c
            else:
                len_ -= 1
                if len_ == 0:
                    break
            s = (s + 1) % N
            r = (r + 1) % N
            tb.insert_node(r)

    payload = writer.flush()
    return struct.pack("<I", original_size) + payload


def decompress(data: bytes, original_size: int | None = None) -> bytes:
    """Decompress LZHUF-compressed *data*.

    If *original_size* is None, the first 4 bytes are read as the
    little-endian original size header.
    """
    offset = 0
    if original_size is None:
        if len(data) < 4:
            raise ValueError("data too short for LZHUF header")
        (original_size,) = struct.unpack_from("<I", data, 0)
        offset = 4

    reader = _BitReader(data[offset:])
    tree   = _HuffmanTree()
    buf    = bytearray(N)

    for i in range(N - F):
        buf[i] = ord(' ')

    r      = N - F
    out    = bytearray()
    count  = 0

    while count < original_size:
        c = tree.decode_char(reader)
        if reader.eof and c == 0:
            break
        if c < 256:
            out.append(c)
            buf[r] = c
            r = (r + 1) % N
            count += 1
        else:
            match_len = c - 255 + THRESHOLD
            match_pos = (r - reader.read_bits(12) - 1) % N
            for k in range(match_len):
                c = buf[(match_pos + k) % N]
                out.append(c)
                buf[r] = c
                r = (r + 1) % N
                count += 1
                if count >= original_size:
                    break

    return bytes(out)
