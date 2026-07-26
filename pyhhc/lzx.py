"""LZX compression for CHM files.

Implements the LZX compression algorithm as used in Microsoft CHM files.
Produces VERBATIM (type 1) blocks. No E8 x86 call translation.
Matches are capped at frame boundaries for decompressor compatibility.
"""

from __future__ import annotations

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


def _init_tables() -> None:
    global _position_base, _extra_bits
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


def _num_position_slots(window_bits: int) -> int:
    return {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}[window_bits]


def _find_position_slot(offset: int) -> int:
    _init_tables()
    if offset < 4:
        return offset
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
        self, window_bits: int = 16, on_frame: Callable[[int, int], None] | None = None
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
        if not data:
            return
        tokens = self._lz(data)
        self._encode(data, tokens)

    def _lz(self, data: bytes) -> list[tuple[int, ...]]:
        n = len(data)
        if n == 0:
            return []

        tokens: list[tuple[int, ...]] = []
        htable: dict[bytes, int] = {}

        r0 = self._r0
        r1 = self._r1
        r2 = self._r2
        uncomp_base = self._uncomp
        next_frame = self._next_frame
        wsize = self.window_size
        append = tokens.append

        i = 0
        while i < n:
            btf = next_frame - uncomp_base - i
            if btf <= 0:
                btf += FRAME_SIZE * ((-btf) // FRAME_SIZE + 1)
            max_len = min(MAX_MATCH, n - i, btf)

            best_len = 1
            best_tok = None

            if max_len >= MIN_MATCH:
                for ri, roff in ((0, r0), (1, r1), (2, r2)):
                    if roff > 0 and i >= roff:
                        s = i - roff
                        ml = 0
                        lim = max_len
                        while ml < lim and data[i + ml] == data[s + ml]:
                            ml += 1
                        if ml >= MIN_MATCH and ml > best_len:
                            best_len = ml
                            best_tok = (_R0 + ri, ml, 0)
                            if ml >= max_len:
                                break

                if best_len < max_len and i + 3 < n:
                    key = data[i : i + 4]
                    p = htable.get(key)
                    htable[key] = i
                    if p is not None:
                        d = i - p
                        if 0 < d <= wsize:
                            ml = min(4, max_len)
                            lim = max_len
                            while ml < lim and data[i + ml] == data[p + ml]:
                                ml += 1
                            if ml >= MIN_MATCH and ml > best_len:
                                fo = d + 2
                                if not (
                                    ml < 3
                                    and fo >= 64
                                    or ml < 4
                                    and fo >= 2048
                                    or ml < 5
                                    and fo >= 65536
                                ):
                                    best_len = ml
                                    best_tok = (_MATCH, ml, d)
                elif i + 3 < n:
                    htable[data[i : i + 4]] = i

            if best_tok is not None:
                tt = best_tok[0]
                ml = best_tok[1]
                if tt == _R0:
                    pass
                elif tt == _R1:
                    r0, r1 = r1, r0
                elif tt == _R2:
                    r0, r2 = r2, r0
                else:
                    r2 = r1
                    r1 = r0
                    r0 = best_tok[2]
                append(best_tok)
                i += ml
            else:
                if i + 3 < n:
                    htable[data[i : i + 4]] = i
                append((_LIT, data[i]))
                i += 1

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
                    else _find_position_slot(tok[2] + 2)
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

        for tok in tokens:
            if tok[0] == _LIT:
                s = tok[1]
                w.write_bits(ml_[s], mc[s])
                self._uncomp += 1
            else:
                ml = tok[1]
                if tok[0] == _MATCH:
                    fo = tok[2] + 2
                    ps = _find_position_slot(fo)
                else:
                    ps = tok[0] - _R0
                    fo = 0

                lh = min(ml - MIN_MATCH, NUM_PRIMARY_LENGTHS)
                ms = NUM_CHARS + ps * 8 + lh
                w.write_bits(ml_[ms], mc[ms])

                if lh == NUM_PRIMARY_LENGTHS:
                    w.write_bits(
                        ll_[ml - MIN_MATCH - NUM_PRIMARY_LENGTHS],
                        lc[ml - MIN_MATCH - NUM_PRIMARY_LENGTHS],
                    )

                eb = _extra_bits[ps]
                if eb > 0:
                    w.write_bits(eb, fo - _position_base[ps])

                self._uncomp += ml

            if self._uncomp >= self._next_frame:
                w.align()
                if self.on_frame:
                    self.on_frame(self._next_frame, w.tell())
                self._next_frame += FRAME_SIZE

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


def lzx_compress(
    data: bytes, window_bits: int = 16, reset_interval: int = 2
) -> tuple[bytes, list[int], int]:
    """Compress data using LZX for CHM.

    Returns (compressed_data, frame_positions, total_uncompressed).
    frame_positions[i] is the compressed byte offset where frame i starts.
    """
    reset_size = reset_interval * FRAME_SIZE
    positions: list[int] = [0]

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
