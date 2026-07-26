"""Full-text search index ($FIftiMain) builder for CHM files.

Implements the FIFTI (Full-text Index To Information) format used by
Microsoft HTML Help Workshop's hhc.exe compiler. Produces the $FIftiMain
internal file that enables search in CHM viewers.

File layout: [Header 1024B] [WLC+Leaf blocks interleaved] [Index blocks]
"""

from __future__ import annotations

import html as html_module
import re
import struct
from io import BytesIO

NODE_SIZE = 4096
HEADER_SIZE = 0x400
MAX_WORD_LEN = 99


def _leb128(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    result = bytearray()
    while value > 0:
        byte = value & 0x7F
        value >>= 7
        if value > 0:
            byte |= 0x80
        result.append(byte)
    return bytes(result)


def _leb128_len(value: int) -> int:
    if value == 0:
        return 1
    n = 0
    while value > 0:
        n += 1
        value >>= 7
    return n


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def _write_sri(value: int, root_size: int) -> tuple[int, int]:
    """Scale/Root integer encoding. Returns (bits_value, bit_count).

    The encoding is a unary prefix (k ones) + 0 separator + mantissa.
    - If value fits in root_size bits: prefix is empty (just 0 + root_size bits)
    - Otherwise: prefix 1-bits indicate how many extra bits beyond root_size
    """
    needed = value.bit_length()
    prefix = max(0, needed - root_size)
    root_bits = max(needed - 1, root_size, 0)

    mask = (1 << root_bits) - 1 if root_bits > 0 else 0
    bits = (~mask) & 0xFFFFFFFF
    bits = (bits << 1) & 0xFFFFFFFF
    bits |= value & mask

    total = prefix + 1 + root_bits
    bits &= (1 << total) - 1
    return bits, total


class _BitWriter:
    """Writes bits MSB-first into a byte buffer, matching the CHM WLC format."""

    __slots__ = ("_buf", "_byte", "_used")

    def __init__(self, buf: bytearray) -> None:
        self._buf = buf
        self._byte = 0
        self._used = 0

    def write(self, value: int, count: int) -> None:
        value = (value << (32 - count)) & 0xFFFFFFFF
        while count > 0:
            needed = 8 - self._used
            tmp = (value >> (24 + self._used)) & 0xFF
            self._byte |= tmp
            consumed = min(count, needed)
            self._used += consumed
            value = (value << consumed) & 0xFFFFFFFF
            count -= consumed
            if self._used == 8:
                self._buf.append(self._byte)
                self._byte = 0
                self._used = 0

    def flush(self) -> None:
        if self._used > 0:
            self._buf.append(self._byte)
            self._byte = 0
            self._used = 0


def _tokenize_html(data: bytes) -> tuple[list[str], list[str]]:
    """Extract words from HTML content.

    Returns (title_words, body_words). Words are lowercased, apostrophes
    stripped, max 99 chars. Matches hhc.exe word-breaking rules.
    """
    try:
        text = data.decode("cp1252", errors="replace")
    except (UnicodeDecodeError, LookupError):
        return [], []

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL
    )
    body_match = re.search(r"<body[^>]*>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)

    def strip_markup(s: str) -> str:
        s = re.sub(
            r"<script[^>]*>.*?</script>", " ", s, flags=re.IGNORECASE | re.DOTALL
        )
        s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.DOTALL)
        s = re.sub(r"<[^>]+>", " ", s)
        s = html_module.unescape(s)
        return s

    def split_words(text: str) -> list[str]:
        text = text.lower()
        words: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            if c.isalnum() or c == "_":
                start = i
                is_number = c.isdigit()
                while i < n:
                    c2 = text[i]
                    if (
                        c2.isalnum()
                        or c2 == "_"
                        or (c2 == "'" and i + 1 < n and text[i + 1].isalnum())
                        or (
                            c2 == "."
                            and is_number
                            and i + 1 < n
                            and text[i + 1].isdigit()
                        )
                    ):
                        i += 1
                    else:
                        break
                word = text[start:i].replace("'", "")
                if word and len(word) <= MAX_WORD_LEN:
                    words.append(word)
            else:
                i += 1
        return words

    title_words: list[str] = []
    if title_match:
        title_words = split_words(strip_markup(title_match.group(1)))

    body_text = strip_markup(body_match.group(1)) if body_match else strip_markup(text)
    body_words = split_words(body_text)

    return title_words, body_words


class _WordEntry:
    """Represents a unique (word, is_title) pair with all document occurrences."""

    __slots__ = ("docs", "is_title", "word")

    def __init__(self, word: str, is_title: bool) -> None:
        self.word = word
        self.is_title = is_title
        self.docs: list[tuple[int, list[int]]] = []

    def add_occurrence(self, doc_idx: int, position: int) -> None:
        if self.docs and self.docs[-1][0] == doc_idx:
            self.docs[-1][1].append(position)
        else:
            self.docs.append((doc_idx, [position]))


def _encode_wlc(
    entry: _WordEntry, doc_root: int, code_root: int, loc_root: int
) -> bytes:
    """Encode WLC (Word, Location, Code) data for a word entry.

    Bit-packed MSB-first with byte-boundary flush between documents.
    """
    buf = bytearray()
    bw = _BitWriter(buf)
    last_doc = 0

    for doc_idx, positions in entry.docs:
        last_loc = 0

        doc_delta = doc_idx - last_doc
        last_doc = doc_idx
        bits, count = _write_sri(doc_delta, doc_root)
        bw.write(bits, count)

        bits, count = _write_sri(len(positions), code_root)
        bw.write(bits, count)

        for loc in positions:
            loc_delta = loc - last_loc
            last_loc = loc
            bits, count = _write_sri(loc_delta, loc_root)
            bw.write(bits, count)

        bw.flush()

    return bytes(buf)


def build_fiftimain(
    html_files: list[tuple[int, bytes]],
    stop_words: set[str],
    codepage: int = 1252,
    locale_id: int = 1033,
) -> bytes:
    """Build the $FIftiMain binary data.

    Args:
        html_files: List of (topic_index, file_data) for each HTML file.
        stop_words: Set of words to exclude from indexing.
        codepage: Windows code page.
        locale_id: Windows locale ID.

    Returns:
        Complete $FIftiMain binary.
    """
    word_map: dict[tuple[str, bool], _WordEntry] = {}
    unique_words: set[str] = set()
    total_word_count = 0
    total_word_length = 0
    longest_word = 0

    for topic_idx, data in html_files:
        title_words, body_words = _tokenize_html(data)

        word_pos = 0
        for word in title_words:
            if word in stop_words:
                continue
            key = (word, True)
            if key not in word_map:
                word_map[key] = _WordEntry(word, True)
                longest_word = max(longest_word, len(word))
            unique_words.add(word)
            word_map[key].add_occurrence(topic_idx, word_pos)
            total_word_count += 1
            total_word_length += len(word)
            word_pos += 1

        for word in body_words:
            if word in stop_words:
                continue
            key = (word, False)
            if key not in word_map:
                word_map[key] = _WordEntry(word, False)
                longest_word = max(longest_word, len(word))
            unique_words.add(word)
            word_map[key].add_occurrence(topic_idx, word_pos)
            total_word_count += 1
            total_word_length += len(word)
            word_pos += 1

    if not word_map:
        return b""

    # Sort: alphabetically, then title (True) before body (False)
    sorted_entries = sorted(word_map.values(), key=lambda e: (e.word, not e.is_title))

    doc_root_size = 1
    code_root_size = 1
    loc_root_size = 6

    # --- Stream-based output ---
    # Architecture: header placeholder, then for each leaf block:
    #   [WLC data for words in this leaf] [leaf block 4096 bytes]
    # Then index blocks at the end.
    out = BytesIO()
    out.write(b"\x00" * HEADER_SIZE)

    # Leaf block state
    leaf_buf = bytearray()
    leaf_buf.extend(struct.pack("<IHH", 0, 0, 0))  # NextLeaf, reserved, FreeSpace
    last_word = ""
    leaf_count = 0
    last_leaf_offset = 0
    free_space = 0
    total_free_space = 0
    index_entries: list[tuple[str, int]] = []

    def _can_hold(word: str) -> bool:
        return len(leaf_buf) + 17 + len(word) < NODE_SIZE

    def _flush_leaf(new_needed: bool) -> None:
        nonlocal \
            leaf_count, \
            last_leaf_offset, \
            last_word, \
            free_space, \
            total_free_space, \
            leaf_buf

        leaf_count += 1
        leaf_offset = out.tell()

        if last_leaf_offset > 0:
            old_pos = out.tell()
            out.seek(last_leaf_offset)
            out.write(struct.pack("<I", leaf_offset))
            out.seek(old_pos)

        free_space = NODE_SIZE - len(leaf_buf)
        total_free_space += free_space
        struct.pack_into("<H", leaf_buf, 6, free_space)

        leaf_buf.extend(b"\x00" * (NODE_SIZE - len(leaf_buf)))
        out.write(bytes(leaf_buf))

        if new_needed:
            index_entries.append((last_word, leaf_offset))

        last_leaf_offset = leaf_offset
        leaf_buf = bytearray()
        leaf_buf.extend(struct.pack("<IHH", 0, 0, 0))
        last_word = ""

    for entry in sorted_entries:
        if not _can_hold(entry.word) and len(leaf_buf) > 8:
            _flush_leaf(True)

        # Compute prefix compression against current last_word
        pfx = _common_prefix_len(entry.word, last_word)
        suffix = entry.word[pfx:]

        # Encode WLC data and write to stream
        wlc_bytes = _encode_wlc(entry, doc_root_size, code_root_size, loc_root_size)
        wlc_offset = out.tell()
        out.write(wlc_bytes)
        wlc_size = len(wlc_bytes)

        # Build leaf entry into the block buffer
        suffix_bytes = suffix.encode("latin-1", errors="replace")
        doc_count_leb = _leb128(len(entry.docs))
        wlc_size_leb = _leb128(wlc_size)

        leaf_buf.append(len(suffix_bytes) + 1)
        leaf_buf.append(pfx)
        leaf_buf.extend(suffix_bytes)
        leaf_buf.append(1 if entry.is_title else 0)
        leaf_buf.extend(doc_count_leb)
        leaf_buf.extend(struct.pack("<I", wlc_offset))
        leaf_buf.extend(struct.pack("<H", 0))
        leaf_buf.extend(wlc_size_leb)

        last_word = entry.word

    # Flush final leaf
    if len(leaf_buf) > 8:
        has_parent = leaf_count > 0
        if has_parent:
            index_entries.append((last_word, out.tell()))
        _flush_leaf(False)

    # --- Write index blocks ---
    tree_depth = 1

    if leaf_count > 1:
        idx_buf = bytearray()
        idx_buf.extend(struct.pack("<H", 0))  # FreeSpace placeholder
        idx_last_word = ""

        for word, child_offset in index_entries:
            pfx = _common_prefix_len(word, idx_last_word)
            suffix = word[pfx:]
            suffix_bytes = suffix.encode("latin-1", errors="replace")

            entry_size = 1 + 1 + len(suffix_bytes) + 4 + 2
            if len(idx_buf) + entry_size > NODE_SIZE and len(idx_buf) > 2:
                idx_free = NODE_SIZE - len(idx_buf)
                struct.pack_into("<H", idx_buf, 0, idx_free)
                idx_buf.extend(b"\x00" * (NODE_SIZE - len(idx_buf)))
                out.write(bytes(idx_buf))
                idx_buf = bytearray()
                idx_buf.extend(struct.pack("<H", 0))
                idx_last_word = ""
                pfx = 0
                suffix = word
                suffix_bytes = suffix.encode("latin-1", errors="replace")

            idx_buf.append(len(suffix_bytes) + 1)
            idx_buf.append(pfx)
            idx_buf.extend(suffix_bytes)
            idx_buf.extend(struct.pack("<I", child_offset))
            idx_buf.extend(struct.pack("<H", 0))
            idx_last_word = word

        if len(idx_buf) > 2:
            idx_free = NODE_SIZE - len(idx_buf)
            struct.pack_into("<H", idx_buf, 0, idx_free)
            idx_buf.extend(b"\x00" * (NODE_SIZE - len(idx_buf)))
            out.write(bytes(idx_buf))

        tree_depth = 2

    # --- Write header ---
    root_offset = out.tell() - NODE_SIZE
    file_count = len(html_files)

    header = bytearray(HEADER_SIZE)
    header[2] = 0x28  # signature byte
    struct.pack_into("<I", header, 0x04, file_count)
    struct.pack_into("<I", header, 0x08, root_offset)
    struct.pack_into("<I", header, 0x0C, 0)
    struct.pack_into("<I", header, 0x10, leaf_count)
    struct.pack_into("<I", header, 0x14, root_offset)
    struct.pack_into("<H", header, 0x18, tree_depth)
    struct.pack_into("<I", header, 0x1A, 7)
    header[0x1E] = 2
    header[0x1F] = doc_root_size
    header[0x20] = 2
    header[0x21] = code_root_size
    header[0x22] = 2
    header[0x23] = loc_root_size
    struct.pack_into("<I", header, 0x2E, NODE_SIZE)
    struct.pack_into("<I", header, 0x32, 1)
    struct.pack_into("<I", header, 0x36, 1)
    struct.pack_into("<I", header, 0x3A, 5)
    struct.pack_into("<I", header, 0x3E, longest_word)
    total_different_words = len(unique_words)
    total_different_word_length = sum(len(e.word) for e in sorted_entries)
    struct.pack_into("<I", header, 0x42, total_word_count)
    struct.pack_into("<I", header, 0x46, total_different_words)
    struct.pack_into("<I", header, 0x4A, total_word_length)
    struct.pack_into("<I", header, 0x4E, 0)
    struct.pack_into("<I", header, 0x52, total_different_word_length)
    struct.pack_into("<I", header, 0x56, total_free_space)
    struct.pack_into("<I", header, 0x5A, 0)
    struct.pack_into("<I", header, 0x5E, max(file_count - 1, 0))
    struct.pack_into("<I", header, 0x7A, codepage)
    struct.pack_into("<I", header, 0x7E, locale_id)

    out.seek(0)
    out.write(header)

    return out.getvalue()
