"""LZX compression for CHM files.

Implements the LZX compression algorithm as used in Microsoft CHM files.
Produces VERBATIM (type 1) blocks. No E8 x86 call translation.
Matches are capped at frame boundaries for decompressor compatibility.

Match finding is delegated to zlib (C speed): each reset-interval block
is deflated, the DEFLATE stream is parsed back into its LZ77 token
sequence, and the tokens are re-encoded as LZX. DEFLATE's 32KB window
and 3..258 match lengths fit inside LZX's limits, so every token
translates; repeated distances are mapped onto LZX repeat offsets.
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Callable

NUM_CHARS = 256
MIN_MATCH = 2
MAX_MATCH = 257
NUM_PRIMARY_LENGTHS = 7
NUM_SECONDARY_LENGTHS = 249
PRETREE_SIZE = 20
MAX_CODE_LENGTH = 16
FRAME_SIZE = 0x8000

_position_base: list[int] = []
_extra_bits: list[int] = []
_slot_table: list[int] = []


def _init_tables() -> None:
    global _position_base, _extra_bits, _slot_table
    if _position_base:
        return
    _position_base = [0] * 290
    _extra_bits = [0] * 290
    j = 0
    for i in range(4):
        _extra_bits[i] = 0
        _position_base[i] = j
        j += 1
    for i in range(4, 36):
        _extra_bits[i] = (i // 2) - 1
        _position_base[i] = j
        j += 1 << _extra_bits[i]
    for i in range(36, 290):
        _extra_bits[i] = 17
        _position_base[i] = j
        j += 1 << 17
    # Direct slot lookup for formatted offsets below 0x20000 (covers the
    # 64KB window used for CHM); larger offsets fall back to search.
    _slot_table = [0] * 0x20000
    s = 0
    for fo in range(0x20000):
        while s + 1 < len(_position_base) and _position_base[s + 1] <= fo:
            s += 1
        _slot_table[fo] = s


def _num_position_slots(window_bits: int) -> int:
    return {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}[window_bits]


def _find_position_slot(offset: int) -> int:
    _init_tables()
    if offset < 0x20000:
        return _slot_table[offset]
    if offset >= 262144:
        return (offset >> 17) + 34
    lo, hi = 4, len(_position_base) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _position_base[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


class _BitWriter:
    __slots__ = ("_bitbuf", "_bitcount", "buf")

    def __init__(self) -> None:
        self.buf = bytearray()
        self._bitbuf = 0
        self._bitcount = 0

    def write_bits(self, nbits: int, value: int) -> None:
        self._bitbuf = (self._bitbuf << nbits) | (value & ((1 << nbits) - 1))
        self._bitcount += nbits
        while self._bitcount >= 16:
            self._bitcount -= 16
            word = (self._bitbuf >> self._bitcount) & 0xFFFF
            self.buf.append(word & 0xFF)
            self.buf.append((word >> 8) & 0xFF)
        if self._bitcount > 0:
            self._bitbuf &= (1 << self._bitcount) - 1
        else:
            self._bitbuf = 0

    def align(self) -> None:
        if self._bitcount > 0:
            self.write_bits(16 - self._bitcount, 0)

    def tell(self) -> int:
        return len(self.buf)


def _build_huffman(freqs: list[int]) -> tuple[list[int], list[int]]:
    """Build canonical Huffman codes from frequency table.

    Returns (code_lengths, codes) indexed by symbol.
    Uses standard canonical assignment (ascending symbol for each length)
    to match decoders.
    """
    n = len(freqs)
    lengths = [0] * n
    codes = [0] * n

    non_zero = [(freqs[i], i) for i in range(n) if freqs[i] > 0]
    if len(non_zero) == 0:
        return lengths, codes

    if len(non_zero) == 1:
        sym = non_zero[0][1]
        lengths[sym] = 1
        dummy = 0 if sym != 0 else 1
        lengths[dummy] = 1
        _assign_codes(lengths, codes, n)
        return lengths, codes

    working = freqs[:]
    while True:
        active = [(working[i], i) for i in range(n) if working[i] > 0]
        active.sort()
        nc = len(active)
        total = 2 * nc - 1
        freq_arr = [0] * total
        par = [0] * total
        for idx in range(nc):
            freq_arr[idx] = active[idx][0]

        q1_start = 0
        q1_end = nc
        q1_arr = list(range(nc))
        q2_arr: list[int] = []
        q2_start = 0
        nxt = nc

        for _ in range(nc - 1):
            if q1_start < q1_end and q2_start < len(q2_arr):
                if freq_arr[q1_arr[q1_start]] <= freq_arr[q2_arr[q2_start]]:
                    a = q1_arr[q1_start]
                    q1_start += 1
                else:
                    a = q2_arr[q2_start]
                    q2_start += 1
            elif q1_start < q1_end:
                a = q1_arr[q1_start]
                q1_start += 1
            else:
                a = q2_arr[q2_start]
                q2_start += 1

            if q1_start < q1_end and q2_start < len(q2_arr):
                if freq_arr[q1_arr[q1_start]] <= freq_arr[q2_arr[q2_start]]:
                    b = q1_arr[q1_start]
                    q1_start += 1
                else:
                    b = q2_arr[q2_start]
                    q2_start += 1
            elif q1_start < q1_end:
                b = q1_arr[q1_start]
                q1_start += 1
            else:
                b = q2_arr[q2_start]
                q2_start += 1

            par[a] = nxt
            par[b] = nxt
            freq_arr[nxt] = freq_arr[a] + freq_arr[b]
            q2_arr.append(nxt)
            nxt += 1

        depth = [0] * total
        for nd in range(total - 2, -1, -1):
            depth[nd] = depth[par[nd]] + 1

        ok = True
        for i in range(nc):
            d = depth[i]
            if d > MAX_CODE_LENGTH:
                ok = False
                break

        if ok:
            for i in range(nc):
                lengths[active[i][1]] = depth[i]
            break

        for i in range(n):
            if working[i] > 0:
                working[i] = max(1, working[i] // 2)

    _assign_codes(lengths, codes, n)
    return lengths, codes


def _assign_codes(lengths: list[int], codes: list[int], n: int) -> None:
    syms = [(lengths[i], i) for i in range(n) if lengths[i] > 0]
    syms.sort()
    if not syms:
        return
    cur_code = 0
    cur_len = syms[0][0]
    for length, sym in syms:
        while length > cur_len:
            cur_code <<= 1
            cur_len += 1
        codes[sym] = cur_code
        cur_code += 1


_LIT = 0
_R0 = 1
_R1 = 2
_R2 = 3
_MATCH = 4

# -- DEFLATE stream parsing (RFC 1951) ------------------------------------
# zlib does the match finding at C speed; we only decode its token stream.

_LEN_BASE = (3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43,
             51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258)
_LEN_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4,
              4, 4, 4, 5, 5, 5, 5, 0)
_DIST_BASE = (1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257,
              385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289,
              16385, 24577)
_DIST_EXTRA = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9,
               9, 10, 10, 11, 11, 12, 12, 13, 13)
_CLEN_ORDER = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1,
               15)

_DECODE_ROOT = 10

_FIXED_LIT_LENGTHS = [8] * 144 + [9] * 112 + [7] * 24 + [8] * 8
_FIXED_DIST_LENGTHS = [5] * 30


def _bitrev(v: int, n: int) -> int:
    r = 0
    for _ in range(n):
        r = (r << 1) | (v & 1)
        v >>= 1
    return r


def _build_decode_table(
    lengths: list[int],
) -> tuple[list[tuple[int, int] | None], dict[tuple[int, int], int]]:
    """Build a flat lookup table for the first _DECODE_ROOT bits; codes
    longer than that go into a dict keyed by (length, code)."""
    table: list[tuple[int, int] | None] = [None] * (1 << _DECODE_ROOT)
    longer: dict[tuple[int, int], int] = {}
    max_len = max(lengths) if lengths else 0
    bl_count = [0] * (max_len + 1)
    for length in lengths:
        if length:
            bl_count[length] += 1
    code = 0
    next_code = [0] * (max_len + 2)
    for bits in range(1, max_len + 1):
        code = (code + bl_count[bits - 1]) << 1
        next_code[bits] = code
    for sym, length in enumerate(lengths):
        if not length:
            continue
        c = next_code[length]
        next_code[length] += 1
        if length <= _DECODE_ROOT:
            rev = _bitrev(c, length)
            step = 1 << length
            for f in range(rev, 1 << _DECODE_ROOT, step):
                table[f] = (sym, length)
        else:
            longer[(length, c)] = sym
    return table, longer


def _read_long_code(
    acc: int, longer: dict[tuple[int, int], int]
) -> tuple[int, int]:
    """Decode a code longer than the root table covers, bit by bit."""
    code = 0
    ln = 0
    while True:
        code = (code << 1) | (acc & 1)
        acc >>= 1
        ln += 1
        if ln > _DECODE_ROOT and (ln, code) in longer:
            return longer[(ln, code)], ln


def _inflate_tokens(comp: bytes) -> list[int | tuple[int, int]]:
    """Parse a raw DEFLATE stream into its LZ77 tokens.

    Returns a list where each element is a literal byte value (int) or a
    (length, distance) pair.
    """
    data = comp + b"\x00" * 8
    pos = 0
    acc = 0
    nbits = 0
    tokens: list[int | tuple[int, int]] = []
    append = tokens.append
    root_mask = (1 << _DECODE_ROOT) - 1

    while True:
        while nbits < 3:
            acc |= data[pos] << nbits
            nbits += 8
            pos += 1
        bfinal = acc & 1
        btype = (acc >> 1) & 3
        acc >>= 3
        nbits -= 3

        if btype == 0:  # stored block, byte-aligned
            pos -= nbits >> 3
            acc = 0
            nbits = 0
            ln = data[pos] | (data[pos + 1] << 8)
            pos += 4
            for k in range(ln):
                append(data[pos + k])
            pos += ln
        else:
            if btype == 1:
                lit_tab, lit_long = _build_decode_table(_FIXED_LIT_LENGTHS)
                dist_tab, dist_long = _build_decode_table(_FIXED_DIST_LENGTHS)
            else:
                while nbits < 14:
                    acc |= data[pos] << nbits
                    nbits += 8
                    pos += 1
                hlit = (acc & 31) + 257
                hdist = ((acc >> 5) & 31) + 1
                hclen = ((acc >> 10) & 15) + 4
                acc >>= 14
                nbits -= 14
                clen = [0] * 19
                for i in range(hclen):
                    while nbits < 3:
                        acc |= data[pos] << nbits
                        nbits += 8
                        pos += 1
                    clen[_CLEN_ORDER[i]] = acc & 7
                    acc >>= 3
                    nbits -= 3
                ctab, clong = _build_decode_table(clen)
                lens: list[int] = []
                while len(lens) < hlit + hdist:
                    while nbits < 22:
                        acc |= data[pos] << nbits
                        nbits += 8
                        pos += 1
                    e = ctab[acc & root_mask]
                    if e is None:
                        sym, ln = _read_long_code(acc, clong)
                    else:
                        sym, ln = e
                    acc >>= ln
                    nbits -= ln
                    if sym < 16:
                        lens.append(sym)
                    elif sym == 16:
                        rep = (acc & 3) + 3
                        acc >>= 2
                        nbits -= 2
                        lens.extend([lens[-1]] * rep)
                    elif sym == 17:
                        rep = (acc & 7) + 3
                        acc >>= 3
                        nbits -= 3
                        lens.extend([0] * rep)
                    else:
                        rep = (acc & 127) + 11
                        acc >>= 7
                        nbits -= 7
                        lens.extend([0] * rep)
                lit_tab, lit_long = _build_decode_table(lens[:hlit])
                dist_tab, dist_long = _build_decode_table(lens[hlit:])

            while True:
                # worst case: length code + extra + dist code + extra
                while nbits < 48:
                    acc |= data[pos] << nbits
                    nbits += 8
                    pos += 1
                e = lit_tab[acc & root_mask]
                if e is None:
                    sym, ln = _read_long_code(acc, lit_long)
                else:
                    sym, ln = e
                acc >>= ln
                nbits -= ln
                if sym < 256:
                    append(sym)
                    continue
                if sym == 256:
                    break
                li = sym - 257
                length = _LEN_BASE[li]
                eb = _LEN_EXTRA[li]
                if eb:
                    length += acc & ((1 << eb) - 1)
                    acc >>= eb
                    nbits -= eb
                e = dist_tab[acc & root_mask]
                if e is None:
                    dsym, ln = _read_long_code(acc, dist_long)
                else:
                    dsym, ln = e
                acc >>= ln
                nbits -= ln
                dist = _DIST_BASE[dsym]
                eb = _DIST_EXTRA[dsym]
                if eb:
                    dist += acc & ((1 << eb) - 1)
                    acc >>= eb
                    nbits -= eb
                append((length, dist))

        if bfinal:
            break
    return tokens


class LZXCompressor:
    def __init__(
        self,
        window_bits: int = 16,
        on_frame: Callable[[int, int], None] | None = None,
    ) -> None:
        _init_tables()
        self.window_bits = window_bits
        self.window_size = 1 << window_bits
        self.num_pos_slots = _num_position_slots(window_bits)
        self.main_tree_size = NUM_CHARS + 8 * self.num_pos_slots
        self.on_frame = on_frame

        self._w = _BitWriter()
        self._prev_main_len: list[int] = [0] * self.main_tree_size
        self._prev_sec_len: list[int] = [0] * NUM_SECONDARY_LENGTHS
        self._r0 = 1
        self._r1 = 1
        self._r2 = 1
        self._uncomp = 0
        self._next_frame = FRAME_SIZE

    def reset(self) -> None:
        self._prev_main_len = [0] * self.main_tree_size
        self._prev_sec_len = [0] * NUM_SECONDARY_LENGTHS
        self._r0 = 1
        self._r1 = 1
        self._r2 = 1
        self._w.write_bits(1, 0)

    def compress(self, data: bytes) -> None:
        """Compress a chunk: zlib finds the matches, we re-encode as LZX."""
        if not data:
            return
        z = zlib.compressobj(9, zlib.DEFLATED, -15)
        stream = z.compress(data) + z.flush()
        tokens = self._convert(data, _inflate_tokens(stream))
        self._encode(data, tokens)

    def _convert(
        self, data: bytes, dtokens: list[int | tuple[int, int]]
    ) -> list[tuple[int, ...]]:
        """Translate DEFLATE tokens to LZX tokens.

        Matches are split at frame boundaries and at LZX's maximum match
        length; distances equal to a repeat-offset register are emitted
        as the corresponding cheap repeat-offset match.
        """
        tokens: list[tuple[int, ...]] = []
        append = tokens.append
        r0 = self._r0
        r1 = self._r1
        r2 = self._r2
        base = self._uncomp
        frame_mask = FRAME_SIZE - 1
        pos = 0
        for t in dtokens:
            if isinstance(t, int):
                append((_LIT, t))
                pos += 1
                continue
            length, dist = t
            while length:
                btf = FRAME_SIZE - ((base + pos) & frame_mask)
                take = min(length, MAX_MATCH, btf)
                if take < MIN_MATCH:
                    append((_LIT, data[pos]))
                    pos += 1
                    length -= 1
                    continue
                if dist == r0:
                    append((_R0, take, 0))
                elif dist == r1:
                    append((_R1, take, 0))
                    r0, r1 = r1, r0
                elif dist == r2:
                    append((_R2, take, 0))
                    r0, r2 = r2, r0
                else:
                    append((_MATCH, take, dist))
                    r2 = r1
                    r1 = r0
                    r0 = dist
                pos += take
                length -= take
        self._r0 = r0
        self._r1 = r1
        self._r2 = r2
        return tokens

    def _encode(self, data: bytes, tokens: list[tuple[int, ...]]) -> None:
        mf = [0] * self.main_tree_size
        lf = [0] * NUM_SECONDARY_LENGTHS

        for tok in tokens:
            if tok[0] == _LIT:
                mf[tok[1]] += 1
            else:
                ml = tok[1]
                ps = (
                    (tok[0] - _R0)
                    if tok[0] != _MATCH
                    else (
                        _slot_table[tok[2] + 2]
                        if tok[2] + 2 < 0x20000
                        else _find_position_slot(tok[2] + 2)
                    )
                )
                lh = min(ml - MIN_MATCH, NUM_PRIMARY_LENGTHS)
                mf[NUM_CHARS + ps * 8 + lh] += 1
                if lh == NUM_PRIMARY_LENGTHS:
                    lf[ml - MIN_MATCH - NUM_PRIMARY_LENGTHS] += 1

        ml_, mc = _build_huffman(mf)
        ll_, lc = _build_huffman(lf)

        w = self._w
        w.write_bits(3, 1)
        w.write_bits(24, len(data))

        self._write_tree(ml_[:NUM_CHARS], self._prev_main_len[:NUM_CHARS])
        self._write_tree(ml_[NUM_CHARS:], self._prev_main_len[NUM_CHARS:])
        self._write_tree(ll_, self._prev_sec_len)

        # Token loop with the bit writer inlined: this is the hottest part
        # of encoding, so bits are accumulated in locals and flushed to the
        # buffer 16 at a time, syncing with the writer only at frame ends.
        buf = w.buf
        append_byte = buf.append
        bitbuf = w._bitbuf
        bitcount = w._bitcount
        uncomp = self._uncomp
        next_frame = self._next_frame
        on_frame = self.on_frame
        slot_table = _slot_table
        eb_table = _extra_bits
        base_table = _position_base

        for tok in tokens:
            if tok[0] == _LIT:
                s = tok[1]
                bitbuf = (bitbuf << ml_[s]) | mc[s]
                bitcount += ml_[s]
                uncomp += 1
            else:
                ml = tok[1]
                if tok[0] == _MATCH:
                    fo = tok[2] + 2
                    ps = (
                        slot_table[fo]
                        if fo < 0x20000
                        else _find_position_slot(fo)
                    )
                else:
                    ps = tok[0] - _R0
                    fo = 0

                lh = ml - MIN_MATCH
                if lh >= NUM_PRIMARY_LENGTHS:
                    ms = NUM_CHARS + ps * 8 + NUM_PRIMARY_LENGTHS
                    bitbuf = (bitbuf << ml_[ms]) | mc[ms]
                    bitcount += ml_[ms]
                    ls = lh - NUM_PRIMARY_LENGTHS
                    bitbuf = (bitbuf << ll_[ls]) | lc[ls]
                    bitcount += ll_[ls]
                else:
                    ms = NUM_CHARS + ps * 8 + lh
                    bitbuf = (bitbuf << ml_[ms]) | mc[ms]
                    bitcount += ml_[ms]

                eb = eb_table[ps]
                if eb > 0:
                    bitbuf = (bitbuf << eb) | (fo - base_table[ps])
                    bitcount += eb

                uncomp += ml

            while bitcount >= 16:
                bitcount -= 16
                word = (bitbuf >> bitcount) & 0xFFFF
                append_byte(word & 0xFF)
                append_byte(word >> 8)
            bitbuf &= (1 << bitcount) - 1

            if uncomp >= next_frame:
                w._bitbuf = bitbuf
                w._bitcount = bitcount
                w.align()
                bitbuf = w._bitbuf
                bitcount = w._bitcount
                if on_frame:
                    on_frame(next_frame, len(buf))
                next_frame += FRAME_SIZE

        w._bitbuf = bitbuf
        w._bitcount = bitcount
        self._uncomp = uncomp
        self._next_frame = next_frame

        self._prev_main_len = ml_[:]
        self._prev_sec_len = ll_[:]

    def _write_tree(self, cur: list[int], prev: list[int]) -> None:
        n = len(cur)
        deltas = [(prev[i] - cur[i]) % 17 for i in range(n)]

        rle: list[tuple[int, ...]] = []
        i = 0
        while i < n:
            if deltas[i] == 0:
                run = 1
                while i + run < n and deltas[i + run] == 0:
                    run += 1
                while run >= 20:
                    e = min(run, 51)
                    rle.append((18, e - 20))
                    run -= e
                    i += e
                while run >= 4:
                    e = min(run, 19)
                    rle.append((17, e - 4))
                    run -= e
                    i += e
                for _ in range(run):
                    rle.append((0,))
                    i += 1
            else:
                rle.append((deltas[i],))
                i += 1

        pf = [0] * PRETREE_SIZE
        for item in rle:
            pf[item[0]] += 1
        pl, pc = _build_huffman(pf)

        w = self._w
        for v in pl:
            w.write_bits(4, v)
        for item in rle:
            c = item[0]
            w.write_bits(pl[c], pc[c])
            if c == 17:
                w.write_bits(4, item[1])
            elif c == 18:
                w.write_bits(5, item[1])

    def finish(self) -> bytes:
        self._w.align()
        return bytes(self._w.buf)

    @property
    def total_uncompressed(self) -> int:
        return self._uncomp


def _compress_block(
    args: tuple[bytes, int],
) -> tuple[bytes, list[int]]:
    """Compress one reset-interval block as an independent bitstream.

    The LZX state (window, Huffman history, repeat offsets) resets at
    every reset boundary and frames are 16-bit aligned, so per-block
    outputs concatenate into the same stream serial compression makes.
    """
    block, window_bits = args
    positions: list[int] = []

    def on_frame(_uncomp: int, comp: int) -> None:
        positions.append(comp)

    c = LZXCompressor(window_bits, on_frame)
    c._w.write_bits(1, 0)
    c.compress(block)
    return c.finish(), positions


def lzx_compress(
    data: bytes,
    window_bits: int = 16,
    reset_interval: int = 2,
    workers: int | None = 1,
) -> tuple[bytes, list[int], int]:
    """Compress data using LZX for CHM.

    Returns (compressed_data, frame_positions, total_uncompressed).
    frame_positions[i] is the compressed byte offset where frame i starts.

    workers is 1 by default (single process, safe for library use — no
    multiprocessing is ever spawned unless asked for). Pass a count > 1
    to compress reset blocks in parallel, or None to use the CPU count.
    Output is byte-identical either way.
    """
    reset_size = reset_interval * FRAME_SIZE
    blocks = [data[off : off + reset_size] for off in range(0, len(data), reset_size)]

    if workers is None:
        workers = min(os.cpu_count() or 1, 16)

    if workers > 1 and len(blocks) >= 2:
        try:
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(max_workers=min(workers, len(blocks))) as ex:
                results = list(
                    ex.map(
                        _compress_block,
                        ((b, window_bits) for b in blocks),
                        chunksize=4,
                    )
                )
        except (ImportError, OSError, RuntimeError):
            # No usable multiprocessing (sandbox, missing spawn support,
            # broken pool): fall back to single-process compression.
            results = None
        if results is not None:
            positions = [0]
            parts: list[bytes] = []
            base = 0
            for out, rel in results:
                positions.extend(base + p for p in rel)
                base += len(out)
                parts.append(out)
            return b"".join(parts), positions, len(data)

    positions = [0]

    def on_frame(_uncomp: int, comp: int) -> None:
        positions.append(comp)

    c = LZXCompressor(window_bits, on_frame)
    c._w.write_bits(1, 0)

    off = 0
    first = True
    while off < len(data):
        if not first:
            c.reset()
        first = False
        end = min(off + reset_size, len(data))
        c.compress(data[off:end])
        off = end

    compressed = c.finish()
    return compressed, positions, c.total_uncompressed
