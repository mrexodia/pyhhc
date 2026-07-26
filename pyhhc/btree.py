"""Binary keyword index ($WWKeywordLinks) builder for CHM files.

Builds the BTree/Data/Map/Property streams that hhc.exe emits from the
project's .hhk index sitemap. hh.exe uses these for the Index tab and
keyword lookup (even when Binary Index=No).

BTree layout: [header 76B] [listing blocks 2048B ...] [index blocks 2048B ...]
"""

from __future__ import annotations

import struct

BLOCK_SIZE = 2048
HEADER_SIZE = 76

# Per-keyword record in the Data stream.
_DATA_ENTRY = bytes([0, 0, 0, 0, 5, 0, 0, 0, 0x80, 0, 0, 0, 0])

# 32-byte $WWKeywordLinks/Property emitted alongside a populated index.
PROPERTY_DATA = struct.pack("<8I", 0, 0, 0, 0x0C, 1, 1, 0, 0)


def keyword_sort_key(keyword: str) -> tuple:
    """Sort key matching hhc.exe's index collation.

    Leading punctuation is ignored for the primary comparison, which is
    case-insensitive; ties are broken case-sensitively with lowercase
    sorting before uppercase (Win32 word-sort behavior).
    """
    i = 0
    while i < len(keyword) and not keyword[i].isalnum():
        i += 1
    primary = keyword[i:].lower()
    secondary = tuple((c.lower(), 1 if c.isupper() else 0) for c in keyword)
    return (primary, secondary)


def _entry_bytes(keyword: str, topics: list[int], tail: int) -> bytes:
    """Common entry encoding; `tail` is the final dword.

    Listing entries carry (1, data_offset) — the caller appends the extra
    dword — while index entries end with the child block number.
    """
    buf = bytearray()
    buf.extend(keyword.encode("utf-16-le"))
    buf.extend(b"\x00\x00")
    buf.extend(struct.pack("<HH", 0, 0))  # seealso, entry depth
    buf.extend(struct.pack("<II", 0, 0))  # comma char index, reserved
    buf.extend(struct.pack("<I", len(topics)))
    for t in topics:
        buf.extend(struct.pack("<I", t))
    buf.extend(struct.pack("<I", tail))
    return bytes(buf)


def build_keyword_links(
    keywords: list[tuple[str, list[int]]],
    locale_id: int = 1033,
    codepage: int = 1252,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Build ($WWKeywordLinks/BTree, Data, Map, Property).

    Args:
        keywords: (keyword, topic_ids) pairs. Duplicate keywords must already
            be merged; the list is sorted here with hhc.exe's collation.
        locale_id: LCID from the project.
        codepage: Windows code page.
    """
    merged = sorted(keywords, key=lambda kv: keyword_sort_key(kv[0]))

    # --- Listing blocks ---
    listing_blocks: list[list[tuple[str, list[int], bytes]]] = [[]]
    for n, (keyword, topics) in enumerate(merged):
        entry = _entry_bytes(keyword, topics, 1) + struct.pack("<I", 13 * n)
        cur_len = sum(len(e[2]) for e in listing_blocks[-1])
        if listing_blocks[-1] and 12 + cur_len + len(entry) >= BLOCK_SIZE:
            listing_blocks.append([])
        listing_blocks[-1].append((keyword, topics, entry))

    n_listing = len(listing_blocks)
    blocks: list[bytes] = []
    for i, entries in enumerate(listing_blocks):
        block = bytearray(BLOCK_SIZE)
        payload = b"".join(e[2] for e in entries)
        struct.pack_into(
            "<HHii",
            block,
            0,
            BLOCK_SIZE - 12 - len(payload),
            len(entries),
            i - 1,
            i + 1 if i + 1 < n_listing else -1,
        )
        block[12 : 12 + len(payload)] = payload
        blocks.append(bytes(block))

    # --- Index levels ---
    # Each level indexes the blocks of the level below: one entry per block
    # after the first, holding that block's first keyword. A block's header
    # child points at the block preceding its first entry's child.
    level: list[tuple[str, list[int], int]] = [
        (entries[0][0], entries[0][1], n_listing_idx)
        for n_listing_idx, entries in enumerate(listing_blocks)
    ]
    n_levels = 0
    while len(level) > 1:
        n_levels += 1
        # Split entries (skipping the first, covered by header child) into blocks.
        idx_blocks: list[list[tuple[str, list[int], int]]] = [[]]
        for keyword, topics, child in level[1:]:
            entry_len = len(_entry_bytes(keyword, topics, 0))
            cur_len = sum(len(_entry_bytes(k, t, 0)) for k, t, _ in idx_blocks[-1])
            if idx_blocks[-1] and 8 + cur_len + entry_len >= BLOCK_SIZE:
                idx_blocks.append([])
            idx_blocks[-1].append((keyword, topics, child))

        next_level: list[tuple[str, list[int], int]] = []
        for entries in idx_blocks:
            block_nr = len(blocks)
            block = bytearray(BLOCK_SIZE)
            payload = b"".join(_entry_bytes(k, t, child) for k, t, child in entries)
            header_child = entries[0][2] - 1
            struct.pack_into(
                "<HHi",
                block,
                0,
                BLOCK_SIZE - 8 - len(payload),
                len(entries),
                header_child,
            )
            block[8 : 8 + len(payload)] = payload
            blocks.append(bytes(block))
            first_kw, first_topics, _ = entries[0]
            next_level.append((first_kw, first_topics, block_nr))
        # The first block of this level is covered by the next level's header
        # child, mirroring the listing-level structure.
        level = next_level

    n_blocks = len(blocks)
    tree_depth = 1 + n_levels
    root_block = n_blocks - 1 if n_levels else 0

    # --- Header ---
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<HHH", header, 0, 0x293B, 0x0104, BLOCK_SIZE)
    header[6:9] = b"X44"
    struct.pack_into("<IIIiI", header, 22, 0, n_listing - 1, root_block, -1, n_blocks)
    struct.pack_into("<H", header, 42, tree_depth)
    struct.pack_into(
        "<8I", header, 44, len(merged), codepage, locale_id, 1, 10031, 0, 0, 0
    )

    btree = bytes(header) + b"".join(blocks)

    # --- Data: one fixed 13-byte record per keyword ---
    data = _DATA_ENTRY * len(merged)

    # --- Map: (entries before block, block number) per listing block ---
    map_buf = bytearray(struct.pack("<H", n_listing))
    entries_before = 0
    for i, entries in enumerate(listing_blocks):
        map_buf.extend(struct.pack("<II", entries_before, i))
        entries_before += len(entries)

    return btree, data, bytes(map_buf), PROPERTY_DATA
