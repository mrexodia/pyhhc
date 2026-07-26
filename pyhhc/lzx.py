"""LZX compression for CHM files.

Implements the LZX compression algorithm as used in Microsoft CHM files.
Produces VERBATIM (type 1) blocks. No E8 x86 call translation.
Matches are capped at frame boundaries for decompressor compatibility.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable

NUM_CHARS = 256
MIN_MATCH = 2
MAX_MATCH = 257
NUM_PRIMARY_LENGTHS = 7
NUM_SECONDARY_LENGTHS = 249
PRETREE_SIZE = 20
MAX_CODE_LENGTH = 16
FRAME_SIZE = 0x8000

# Match-finder tuning per compression level. Fields: depth (hash-chain
# candidates tried per position), nice (match length that stops the
# search), lazy (match length that skips the lazy one-byte-later probe),
# sample_min/sample_step (inside matches at least sample_min long, only
# every sample_step-th position is added to the hash chains).
LEVEL_FAST = 1
LEVEL_NORMAL = 2
LEVEL_BEST = 3
_LEVELS = {
    LEVEL_FAST: (2, 32, 4, 8, 4),
    LEVEL_NORMAL: (4, 32, 8, 8, 4),
    LEVEL_BEST: (64, 128, 32, 16, 2),
}
# Length above which the chain search runs with reduced depth.
GOOD_LENGTH = 32

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


def _should_reject(ml: int, foff: int) -> bool:
    if ml < 2:
        return True
    if foff >= 64 and ml < 3:
        return True
    if foff >= 2048 and ml < 4:
        return True
    return foff >= 65536 and ml < 5


class LZXCompressor:
    def __init__(
        self,
        window_bits: int = 16,
        on_frame: Callable[[int, int], None] | None = None,
        level: int = LEVEL_NORMAL,
    ) -> None:
        _init_tables()
        self.tuning = _LEVELS[level]
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
        if not data:
            return
        tokens = self._lz(data)
        self._encode(data, tokens)

    def _lz(self, data: bytes) -> list[tuple[int, ...]]:
        n = len(data)
        if n == 0:
            return []

        tokens: list[tuple[int, ...]] = []
        append = tokens.append
        head: dict[int, int] = {}
        prev = [-1] * n
        # Last position with a full 4-byte key (exclusive bound). Keys are
        # the 4 bytes at each position as little-endian ints, built in bulk
        # with four strided unpacks instead of one slice per position.
        nk = n - 3 if n >= 4 else 0
        keys = [0] * nk
        for r in range(min(4, nk)):
            cnt = (nk - r + 3) // 4
            vals = struct.unpack_from(f"<{(n - r) // 4}I", data, r)
            keys[r::4] = vals[:cnt]

        r0 = self._r0
        r1 = self._r1
        r2 = self._r2
        uncomp_base = self._uncomp
        next_frame = self._next_frame
        wsize = self.window_size

        depth_max, nice_len, lazy_skip, sample_min, sample_step = self.tuning
        good_len = GOOD_LENGTH
        head_get = head.get

        def find(i: int, max_len: int) -> tuple[int, int, int]:
            """Best match at i as (length, kind, dist); kind is rep index 0-2
            or 3 for a normal match, -1 for none (length 0)."""
            best_len = MIN_MATCH - 1
            kind = -1
            dist = 0
            for ri, roff in ((0, r0), (1, r1), (2, r2)):
                if 0 < roff <= i:
                    s = i - roff
                    if data[i] != data[s]:
                        continue
                    ml = 0
                    while (
                        ml + 8 <= max_len
                        and data[i + ml : i + ml + 8] == data[s + ml : s + ml + 8]
                    ):
                        ml += 8
                    while ml < max_len and data[i + ml] == data[s + ml]:
                        ml += 1
                    if ml > best_len:
                        best_len = ml
                        kind = ri
                        if ml >= max_len:
                            return best_len, kind, 0
            if i < nk and best_len < max_len:
                depth = depth_max if best_len < good_len else depth_max >> 2
                p = head_get(keys[i], -1)
                while p >= 0 and depth > 0:
                    if i - p > wsize:
                        break
                    if data[p + best_len] == data[i + best_len]:
                        ml = 0
                        while (
                            ml + 8 <= max_len
                            and data[i + ml : i + ml + 8] == data[p + ml : p + ml + 8]
                        ):
                            ml += 8
                        while ml < max_len and data[i + ml] == data[p + ml]:
                            ml += 1
                        # A repeat offset is much cheaper to encode than a
                        # far normal match, so require a margin to displace
                        # a rep candidate.
                        if ml > best_len + (1 if 0 <= kind <= 2 else 0):
                            d = i - p
                            if not _should_reject(ml, d + 2):
                                best_len = ml
                                kind = 3
                                dist = d
                                if ml >= nice_len or best_len >= max_len:
                                    break
                    p = prev[p]
                    depth -= 1
            if kind < 0 or best_len < MIN_MATCH:
                return 0, -1, 0
            return best_len, kind, dist

        i = 0
        have_prev = False
        prev_len = 0
        prev_kind = -1
        prev_dist = 0
        while i < n:
            btf = next_frame - uncomp_base - i
            if btf <= 0:
                btf += FRAME_SIZE * ((-btf) // FRAME_SIZE + 1)
            max_len = min(MAX_MATCH, n - i, btf)

            cur_len = 0
            cur_kind = -1
            cur_dist = 0
            if max_len >= MIN_MATCH:
                # Only run the full search when either the hash table or a
                # repeat offset can possibly match here.
                b = data[i]
                if (
                    (i < nk and head_get(keys[i], -1) >= 0)
                    or (0 < r0 <= i and b == data[i - r0])
                    or (0 < r1 <= i and b == data[i - r1])
                    or (0 < r2 <= i and b == data[i - r2])
                ):
                    cur_len, cur_kind, cur_dist = find(i, max_len)

            if have_prev and prev_len >= cur_len and prev_len >= MIN_MATCH:
                # The match found at i-1 wins over the one here: emit it.
                if prev_kind == 3:
                    r2 = r1
                    r1 = r0
                    r0 = prev_dist
                    append((_MATCH, prev_len, prev_dist))
                elif prev_kind == 0:
                    append((_R0, prev_len, 0))
                elif prev_kind == 1:
                    r0, r1 = r1, r0
                    append((_R1, prev_len, 0))
                else:
                    r0, r2 = r2, r0
                    append((_R2, prev_len, 0))
                end = i - 1 + prev_len
                lim = min(end - 1, nk - 1)
                step = 1 if prev_len < sample_min else sample_step
                for j in range(i, lim + 1, step):
                    k = keys[j]
                    prev[j] = head_get(k, -1)
                    head[k] = j
                i = end
                have_prev = False
            elif not have_prev and cur_len >= lazy_skip:
                # Long enough that a one-byte-later improvement is unlikely:
                # emit directly and skip the lazy probe at i+1.
                if cur_kind == 3:
                    r2 = r1
                    r1 = r0
                    r0 = cur_dist
                    append((_MATCH, cur_len, cur_dist))
                elif cur_kind == 0:
                    append((_R0, cur_len, 0))
                elif cur_kind == 1:
                    r0, r1 = r1, r0
                    append((_R1, cur_len, 0))
                else:
                    r0, r2 = r2, r0
                    append((_R2, cur_len, 0))
                end = i + cur_len
                lim = min(end - 1, nk - 1)
                step = 1 if cur_len < sample_min else sample_step
                for j in range(i, lim + 1, step):
                    k = keys[j]
                    prev[j] = head_get(k, -1)
                    head[k] = j
                i = end
            else:
                if have_prev:
                    append((_LIT, data[i - 1]))
                elif cur_len < MIN_MATCH:
                    # No pending token and nothing found here: emit directly
                    # instead of carrying a known-empty pending slot.
                    append((_LIT, data[i]))
                    if i < nk:
                        k = keys[i]
                        prev[i] = head_get(k, -1)
                        head[k] = i
                    i += 1
                    continue
                have_prev = True
                prev_len, prev_kind, prev_dist = cur_len, cur_kind, cur_dist
                if i < nk:
                    k = keys[i]
                    prev[i] = head_get(k, -1)
                    head[k] = i
                i += 1

        if have_prev:
            append((_LIT, data[n - 1]))

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
    args: tuple[bytes, int, int],
) -> tuple[bytes, list[int]]:
    """Compress one reset-interval block as an independent bitstream.

    The LZX state (window, Huffman history, repeat offsets) resets at
    every reset boundary and frames are 16-bit aligned, so per-block
    outputs concatenate into the same stream serial compression makes.
    """
    block, window_bits, level = args
    positions: list[int] = []

    def on_frame(_uncomp: int, comp: int) -> None:
        positions.append(comp)

    c = LZXCompressor(window_bits, on_frame, level)
    c._w.write_bits(1, 0)
    c.compress(block)
    return c.finish(), positions


def lzx_compress(
    data: bytes,
    window_bits: int = 16,
    reset_interval: int = 2,
    level: int = LEVEL_NORMAL,
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
                        ((b, window_bits, level) for b in blocks),
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

    c = LZXCompressor(window_bits, on_frame, level)
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
