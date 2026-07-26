"""CHM (Compiled HTML Help) file writer."""

from __future__ import annotations

import os
import re
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .btree import PROPERTY_DATA, build_keyword_links
from .fts import build_fiftimain
from .lzx import FRAME_SIZE, lzx_compress
from .project import HHPProject
from .sitemap import SiteMapItem, parse_sitemap

ITSF_SIGNATURE = b"ITSF"
ITSP_SIGNATURE = b"ITSP"
PMGL_SIGNATURE = b"PMGL"
PMGI_SIGNATURE = b"PMGI"

GUID1 = bytes(
    [
        0x10,
        0xFD,
        0x01,
        0x7C,
        0xAA,
        0x7B,
        0xD0,
        0x11,
        0x9E,
        0x0C,
        0x00,
        0xA0,
        0xC9,
        0x22,
        0xE6,
        0xEC,
    ]
)
GUID2 = bytes(
    [
        0x11,
        0xFD,
        0x01,
        0x7C,
        0xAA,
        0x7B,
        0xD0,
        0x11,
        0x9E,
        0x0C,
        0x00,
        0xA0,
        0xC9,
        0x22,
        0xE6,
        0xEC,
    ]
)
ITSP_GUID = bytes(
    [
        0x6A,
        0x92,
        0x02,
        0x5D,
        0x2E,
        0x21,
        0xD0,
        0x11,
        0x9D,
        0xF9,
        0x00,
        0xA0,
        0xC9,
        0x22,
        0xE6,
        0xEC,
    ]
)
TRANSFORM_GUID = b"{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}"
TRANSFORM_GUID_BYTES = bytes(
    [
        0x40,
        0x89,
        0xC2,
        0x7F,
        0x31,
        0x9D,
        0xD0,
        0x11,
        0x9B,
        0x27,
        0x00,
        0xA0,
        0xC9,
        0x1E,
        0x9C,
        0x7C,
    ]
)

CHUNK_SIZE = 0x1000
# ITSP density field is 2, giving 1 + (1 << 2) = 5: every 5th entry gets a
# quickref slot. ITSS derives the quickref count from this, so it is not
# optional — chunk packing must reserve room for it.
QUICKREF_DENSITY = 5
LZX_WINDOW_BITS = 16
LZX_RESET_INTERVAL = 2
COMPILER_VERSION = "HHA Version 4.74.8702"

# $OBJINST character classification table (256 entries x 10 bytes)
# From chmobjinstconst.inc in the Free Pascal CHM builder
OBJINST_CHAR_TABLE = bytes(
    [
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x07,
        0x00,
        0x01,
        0x00,
        0x01,
        0x01,
        0x01,
        0x01,
        0x00,
        0x00,
        0x00,
        0x00,
        0x02,
        0x00,
        0x02,
        0x02,
        0x02,
        0x02,
        0x00,
        0x00,
        0x00,
        0x00,
        0x03,
        0x00,
        0x03,
        0x03,
        0x03,
        0x03,
        0x00,
        0x00,
        0x00,
        0x00,
        0x04,
        0x00,
        0x04,
        0x04,
        0x04,
        0x04,
        0x00,
        0x00,
        0x00,
        0x00,
        0x05,
        0x00,
        0x05,
        0x05,
        0x05,
        0x05,
        0x00,
        0x00,
        0x00,
        0x00,
        0x06,
        0x00,
        0x06,
        0x06,
        0x06,
        0x06,
        0x00,
        0x00,
        0x00,
        0x00,
        0x07,
        0x00,
        0x07,
        0x07,
        0x07,
        0x07,
        0x00,
        0x00,
        0x00,
        0x00,
        0x08,
        0x00,
        0x08,
        0x08,
        0x08,
        0x08,
        0x00,
        0x00,
        0x00,
        0x00,
        0x09,
        0x00,
        0x09,
        0x09,
        0x09,
        0x09,
        0x00,
        0x00,
        0x00,
        0x00,
        0x0A,
        0x00,
        0x0A,
        0x0A,
        0x0A,
        0x0A,
        0x00,
        0x00,
        0x00,
        0x00,
        0x0B,
        0x00,
        0x0B,
        0x0B,
        0x0B,
        0x0B,
        0x00,
        0x00,
        0x00,
        0x00,
        0x0C,
        0x00,
        0x0C,
        0x0C,
        0x0C,
        0x0C,
        0x00,
        0x00,
        0x00,
        0x00,
        0x0D,
        0x00,
        0x0D,
        0x0D,
        0x0D,
        0x0D,
        0x00,
        0x00,
        0x00,
        0x00,
        0x0E,
        0x00,
        0x0E,
        0x0E,
        0x14,
        0x14,
        0x00,
        0x00,
        0x00,
        0x00,
        0x0F,
        0x00,
        0x0F,
        0x0F,
        0x0F,
        0x0F,
        0x00,
        0x00,
        0x00,
        0x00,
        0x10,
        0x00,
        0x10,
        0x10,
        0x10,
        0x10,
        0x00,
        0x00,
        0x00,
        0x00,
        0x11,
        0x00,
        0x11,
        0x11,
        0x11,
        0x11,
        0x00,
        0x00,
        0x00,
        0x00,
        0x12,
        0x00,
        0x12,
        0x12,
        0x12,
        0x12,
        0x00,
        0x00,
        0x00,
        0x00,
        0x13,
        0x00,
        0x13,
        0x13,
        0x13,
        0x13,
        0x00,
        0x00,
        0x00,
        0x00,
        0x20,
        0x00,
        0x14,
        0x14,
        0x14,
        0x14,
        0x00,
        0x00,
        0x00,
        0x00,
        0x15,
        0x00,
        0x15,
        0x15,
        0x15,
        0x15,
        0x00,
        0x00,
        0x00,
        0x00,
        0x16,
        0x00,
        0x16,
        0x16,
        0x16,
        0x16,
        0x00,
        0x00,
        0x00,
        0x00,
        0x17,
        0x00,
        0x17,
        0x17,
        0x17,
        0x17,
        0x00,
        0x00,
        0x00,
        0x00,
        0x18,
        0x00,
        0x18,
        0x18,
        0x18,
        0x18,
        0x00,
        0x00,
        0x00,
        0x00,
        0x19,
        0x00,
        0x19,
        0x19,
        0x19,
        0x19,
        0x00,
        0x00,
        0x00,
        0x00,
        0x1A,
        0x00,
        0x1A,
        0x1A,
        0x1A,
        0x1A,
        0x00,
        0x00,
        0x00,
        0x00,
        0x1B,
        0x00,
        0x1B,
        0x1B,
        0x1B,
        0x1B,
        0x00,
        0x00,
        0x00,
        0x00,
        0x1C,
        0x00,
        0x1C,
        0x1C,
        0x1C,
        0x1C,
        0x00,
        0x00,
        0x00,
        0x00,
        0x1D,
        0x00,
        0x1D,
        0x1D,
        0x1D,
        0x1D,
        0x00,
        0x00,
        0x00,
        0x00,
        0x1E,
        0x00,
        0x1E,
        0x1E,
        0x1E,
        0x1E,
        0x00,
        0x00,
        0x00,
        0x00,
        0x1F,
        0x00,
        0x1F,
        0x1F,
        0x1F,
        0x1F,
        0x00,
        0x00,
        0x00,
        0x00,
        0x20,
        0x00,
        0x20,
        0x20,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0x23,
        0x00,
        0x21,
        0x21,
        0x21,
        0x21,
        0x00,
        0x00,
        0x00,
        0x00,
        0x28,
        0x00,
        0x22,
        0x22,
        0x22,
        0x22,
        0x00,
        0x00,
        0x00,
        0x00,
        0x2D,
        0x00,
        0x23,
        0x23,
        0x23,
        0x23,
        0x00,
        0x00,
        0x00,
        0x00,
        0x32,
        0x00,
        0x24,
        0x24,
        0x24,
        0x24,
        0x00,
        0x00,
        0x00,
        0x00,
        0x37,
        0x00,
        0x25,
        0x25,
        0x25,
        0x25,
        0x00,
        0x00,
        0x00,
        0x00,
        0x3C,
        0x00,
        0x26,
        0x26,
        0x26,
        0x26,
        0x00,
        0x00,
        0x06,
        0x00,
        0x41,
        0x00,
        0x27,
        0x27,
        0x27,
        0x27,
        0x00,
        0x00,
        0x00,
        0x00,
        0x46,
        0x00,
        0x28,
        0x28,
        0x28,
        0x28,
        0x00,
        0x00,
        0x00,
        0x00,
        0x4B,
        0x00,
        0x29,
        0x29,
        0x29,
        0x29,
        0x00,
        0x00,
        0x09,
        0x00,
        0x50,
        0x00,
        0x2A,
        0x2A,
        0x2A,
        0x2A,
        0x00,
        0x00,
        0x00,
        0x00,
        0x55,
        0x00,
        0x2B,
        0x2B,
        0x2B,
        0x2B,
        0x00,
        0x00,
        0x04,
        0x00,
        0x5A,
        0x00,
        0x2C,
        0x2C,
        0x2C,
        0x2C,
        0x00,
        0x00,
        0x00,
        0x00,
        0x5F,
        0x00,
        0x2D,
        0x2D,
        0x2D,
        0x2D,
        0x00,
        0x00,
        0x05,
        0x00,
        0x64,
        0x00,
        0x2E,
        0x2E,
        0x2E,
        0x2E,
        0x00,
        0x00,
        0x00,
        0x00,
        0x69,
        0x00,
        0x2F,
        0x2F,
        0x2F,
        0x2F,
        0x00,
        0x00,
        0x03,
        0x00,
        0x60,
        0x04,
        0x30,
        0x30,
        0x30,
        0x30,
        0x00,
        0x00,
        0x03,
        0x00,
        0x6A,
        0x04,
        0x31,
        0x31,
        0x31,
        0x31,
        0x00,
        0x00,
        0x03,
        0x00,
        0x74,
        0x04,
        0x32,
        0x32,
        0x32,
        0x32,
        0x00,
        0x00,
        0x03,
        0x00,
        0x7E,
        0x04,
        0x33,
        0x33,
        0x33,
        0x33,
        0x00,
        0x00,
        0x03,
        0x00,
        0x88,
        0x04,
        0x34,
        0x34,
        0x34,
        0x34,
        0x00,
        0x00,
        0x03,
        0x00,
        0x92,
        0x04,
        0x35,
        0x35,
        0x35,
        0x35,
        0x00,
        0x00,
        0x03,
        0x00,
        0x9C,
        0x04,
        0x36,
        0x36,
        0x36,
        0x36,
        0x00,
        0x00,
        0x03,
        0x00,
        0xA6,
        0x04,
        0x37,
        0x37,
        0x37,
        0x37,
        0x00,
        0x00,
        0x03,
        0x00,
        0xB0,
        0x04,
        0x38,
        0x38,
        0x38,
        0x38,
        0x00,
        0x00,
        0x03,
        0x00,
        0xBA,
        0x04,
        0x39,
        0x39,
        0x39,
        0x39,
        0x00,
        0x00,
        0x00,
        0x00,
        0x6E,
        0x00,
        0x3A,
        0x3A,
        0x3A,
        0x3A,
        0x00,
        0x00,
        0x00,
        0x00,
        0x73,
        0x00,
        0x3B,
        0x3B,
        0x3B,
        0x3B,
        0x00,
        0x00,
        0x00,
        0x00,
        0x78,
        0x00,
        0x3C,
        0x3C,
        0x3C,
        0x3C,
        0x00,
        0x00,
        0x00,
        0x00,
        0x7D,
        0x00,
        0x3D,
        0x3D,
        0x3D,
        0x3D,
        0x00,
        0x00,
        0x00,
        0x00,
        0x82,
        0x00,
        0x3E,
        0x3E,
        0x3E,
        0x3E,
        0x00,
        0x00,
        0x09,
        0x00,
        0x87,
        0x00,
        0x3F,
        0x3F,
        0x3F,
        0x3F,
        0x00,
        0x00,
        0x00,
        0x00,
        0x8C,
        0x00,
        0x40,
        0x40,
        0x40,
        0x40,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0x41,
        0x41,
        0x41,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE2,
        0x04,
        0x62,
        0x42,
        0x42,
        0x42,
        0x00,
        0x00,
        0x02,
        0x00,
        0xF6,
        0x04,
        0x63,
        0x43,
        0x43,
        0x43,
        0x00,
        0x00,
        0x02,
        0x00,
        0x0A,
        0x05,
        0x64,
        0x44,
        0x44,
        0x44,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0x45,
        0x45,
        0x45,
        0x00,
        0x00,
        0x02,
        0x00,
        0x32,
        0x05,
        0x66,
        0x46,
        0x46,
        0x46,
        0x00,
        0x00,
        0x02,
        0x00,
        0x46,
        0x05,
        0x67,
        0x47,
        0x47,
        0x47,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5A,
        0x05,
        0x68,
        0x48,
        0x48,
        0x48,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0x49,
        0x49,
        0x49,
        0x00,
        0x00,
        0x02,
        0x00,
        0x82,
        0x05,
        0x6A,
        0x4A,
        0x4A,
        0x4A,
        0x00,
        0x00,
        0x02,
        0x00,
        0x96,
        0x05,
        0x6B,
        0x4B,
        0x4B,
        0x4B,
        0x00,
        0x00,
        0x02,
        0x00,
        0xAA,
        0x05,
        0x6C,
        0x4C,
        0x4C,
        0x4C,
        0x00,
        0x00,
        0x02,
        0x00,
        0xBE,
        0x05,
        0x6D,
        0x4D,
        0x4D,
        0x4D,
        0x00,
        0x00,
        0x02,
        0x00,
        0xD2,
        0x05,
        0x6E,
        0x4E,
        0x4E,
        0x4E,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0x4F,
        0x4F,
        0x4F,
        0x00,
        0x00,
        0x02,
        0x00,
        0xFA,
        0x05,
        0x70,
        0x50,
        0x50,
        0x50,
        0x00,
        0x00,
        0x02,
        0x00,
        0x0E,
        0x06,
        0x71,
        0x51,
        0x51,
        0x51,
        0x00,
        0x00,
        0x02,
        0x00,
        0x22,
        0x06,
        0x72,
        0x52,
        0x52,
        0x52,
        0x00,
        0x00,
        0x02,
        0x00,
        0x36,
        0x06,
        0x73,
        0x53,
        0x53,
        0x53,
        0x00,
        0x00,
        0x02,
        0x00,
        0x4A,
        0x06,
        0x74,
        0x54,
        0x54,
        0x54,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0x55,
        0x55,
        0x55,
        0x00,
        0x00,
        0x02,
        0x00,
        0x72,
        0x06,
        0x76,
        0x56,
        0x56,
        0x56,
        0x00,
        0x00,
        0x02,
        0x00,
        0x86,
        0x06,
        0x77,
        0x57,
        0x57,
        0x57,
        0x00,
        0x00,
        0x02,
        0x00,
        0x9A,
        0x06,
        0x78,
        0x58,
        0x58,
        0x58,
        0x00,
        0x00,
        0x02,
        0x00,
        0xAE,
        0x06,
        0x79,
        0x59,
        0x59,
        0x59,
        0x00,
        0x00,
        0x02,
        0x00,
        0xC2,
        0x06,
        0x7A,
        0x5A,
        0x5A,
        0x5A,
        0x00,
        0x00,
        0x00,
        0x00,
        0x91,
        0x00,
        0x5B,
        0x5B,
        0x5B,
        0x5B,
        0x00,
        0x00,
        0x00,
        0x00,
        0x96,
        0x00,
        0x5C,
        0x5C,
        0x5C,
        0x5C,
        0x00,
        0x00,
        0x00,
        0x00,
        0x9B,
        0x00,
        0x5D,
        0x5D,
        0x5D,
        0x5D,
        0x00,
        0x00,
        0x00,
        0x00,
        0xA0,
        0x00,
        0x5E,
        0x5E,
        0x5E,
        0x5E,
        0x00,
        0x00,
        0x01,
        0x00,
        0xA5,
        0x00,
        0x5F,
        0x5F,
        0x5F,
        0x5F,
        0x00,
        0x00,
        0x00,
        0x00,
        0xAA,
        0x00,
        0x60,
        0x60,
        0x60,
        0x60,
        0x00,
        0x00,
        0x01,
        0x00,
        0xCE,
        0x04,
        0x61,
        0x61,
        0x61,
        0x61,
        0x00,
        0x00,
        0x01,
        0x00,
        0xE2,
        0x04,
        0x62,
        0x62,
        0x62,
        0x62,
        0x00,
        0x00,
        0x01,
        0x00,
        0xF6,
        0x04,
        0x63,
        0x63,
        0x63,
        0x63,
        0x00,
        0x00,
        0x01,
        0x00,
        0x0A,
        0x05,
        0x64,
        0x64,
        0x64,
        0x64,
        0x00,
        0x00,
        0x01,
        0x00,
        0x1E,
        0x05,
        0x65,
        0x65,
        0x65,
        0x65,
        0x00,
        0x00,
        0x01,
        0x00,
        0x32,
        0x05,
        0x66,
        0x66,
        0x66,
        0x66,
        0x00,
        0x00,
        0x01,
        0x00,
        0x46,
        0x05,
        0x67,
        0x67,
        0x67,
        0x67,
        0x00,
        0x00,
        0x01,
        0x00,
        0x5A,
        0x05,
        0x68,
        0x68,
        0x68,
        0x68,
        0x00,
        0x00,
        0x01,
        0x00,
        0x6E,
        0x05,
        0x69,
        0x69,
        0x69,
        0x69,
        0x00,
        0x00,
        0x01,
        0x00,
        0x82,
        0x05,
        0x6A,
        0x6A,
        0x6A,
        0x6A,
        0x00,
        0x00,
        0x01,
        0x00,
        0x96,
        0x05,
        0x6B,
        0x6B,
        0x6B,
        0x6B,
        0x00,
        0x00,
        0x01,
        0x00,
        0xAA,
        0x05,
        0x6C,
        0x6C,
        0x6C,
        0x6C,
        0x00,
        0x00,
        0x01,
        0x00,
        0xBE,
        0x05,
        0x6D,
        0x6D,
        0x6D,
        0x6D,
        0x00,
        0x00,
        0x01,
        0x00,
        0xD2,
        0x05,
        0x6E,
        0x6E,
        0x6E,
        0x6E,
        0x00,
        0x00,
        0x01,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0x6F,
        0x6F,
        0x6F,
        0x00,
        0x00,
        0x01,
        0x00,
        0xFA,
        0x05,
        0x70,
        0x70,
        0x70,
        0x70,
        0x00,
        0x00,
        0x01,
        0x00,
        0x0E,
        0x06,
        0x71,
        0x71,
        0x71,
        0x71,
        0x00,
        0x00,
        0x01,
        0x00,
        0x22,
        0x06,
        0x72,
        0x72,
        0x72,
        0x72,
        0x00,
        0x00,
        0x01,
        0x00,
        0x36,
        0x06,
        0x73,
        0x73,
        0x73,
        0x73,
        0x00,
        0x00,
        0x01,
        0x00,
        0x4A,
        0x06,
        0x74,
        0x74,
        0x74,
        0x74,
        0x00,
        0x00,
        0x01,
        0x00,
        0x5E,
        0x06,
        0x75,
        0x75,
        0x75,
        0x75,
        0x00,
        0x00,
        0x01,
        0x00,
        0x72,
        0x06,
        0x76,
        0x76,
        0x76,
        0x76,
        0x00,
        0x00,
        0x01,
        0x00,
        0x86,
        0x06,
        0x77,
        0x77,
        0x77,
        0x77,
        0x00,
        0x00,
        0x01,
        0x00,
        0x9A,
        0x06,
        0x78,
        0x78,
        0x78,
        0x78,
        0x00,
        0x00,
        0x01,
        0x00,
        0xAE,
        0x06,
        0x79,
        0x79,
        0x79,
        0x79,
        0x00,
        0x00,
        0x01,
        0x00,
        0xC2,
        0x06,
        0x7A,
        0x7A,
        0x7A,
        0x7A,
        0x00,
        0x00,
        0x00,
        0x00,
        0xAF,
        0x00,
        0x7B,
        0x7B,
        0x7B,
        0x7B,
        0x00,
        0x00,
        0x00,
        0x00,
        0xB4,
        0x00,
        0x7C,
        0x7C,
        0x7C,
        0x7C,
        0x00,
        0x00,
        0x00,
        0x00,
        0xB9,
        0x00,
        0x7D,
        0x7D,
        0x7D,
        0x7D,
        0x00,
        0x00,
        0x00,
        0x00,
        0xBE,
        0x00,
        0x7E,
        0x7E,
        0x7E,
        0x7E,
        0x00,
        0x00,
        0x00,
        0x00,
        0xBF,
        0x00,
        0x7F,
        0x7F,
        0x7F,
        0x7F,
        0x00,
        0x00,
        0x00,
        0x00,
        0xC0,
        0x00,
        0x80,
        0x80,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xC1,
        0x00,
        0x81,
        0x81,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xC3,
        0x00,
        0x82,
        0x82,
        0xE2,
        0xE2,
        0x00,
        0x00,
        0x00,
        0x00,
        0xC8,
        0x00,
        0x83,
        0x83,
        0xC4,
        0xC4,
        0x00,
        0x00,
        0x00,
        0x00,
        0xCD,
        0x00,
        0x84,
        0x84,
        0xE3,
        0xE3,
        0x00,
        0x00,
        0x00,
        0x00,
        0xD2,
        0x00,
        0x85,
        0x85,
        0xC9,
        0xC9,
        0x00,
        0x00,
        0x00,
        0x00,
        0xD7,
        0x00,
        0x86,
        0x86,
        0xA0,
        0xA0,
        0x00,
        0x00,
        0x00,
        0x00,
        0xDC,
        0x00,
        0x87,
        0x87,
        0xE0,
        0xE0,
        0x00,
        0x00,
        0x00,
        0x00,
        0xE1,
        0x00,
        0x88,
        0x88,
        0x5E,
        0x5E,
        0x00,
        0x00,
        0x00,
        0x00,
        0xE6,
        0x00,
        0x89,
        0x89,
        0xE4,
        0xE4,
        0x00,
        0x00,
        0x02,
        0x00,
        0x36,
        0x06,
        0x73,
        0x8A,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xF0,
        0x00,
        0x8B,
        0x8B,
        0xDC,
        0xDC,
        0x00,
        0x00,
        0x0C,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0x8C,
        0xCE,
        0xCE,
        0x00,
        0x00,
        0x00,
        0x00,
        0xF6,
        0x00,
        0x8D,
        0x8D,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xF7,
        0x00,
        0x8E,
        0x8E,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xF8,
        0x00,
        0x8F,
        0x8F,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xF9,
        0x00,
        0x90,
        0x90,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x04,
        0xFA,
        0x00,
        0x91,
        0x91,
        0xD4,
        0xD4,
        0x00,
        0x00,
        0x00,
        0x05,
        0xFF,
        0x00,
        0x92,
        0x92,
        0xD5,
        0xD5,
        0x00,
        0x00,
        0x00,
        0x06,
        0x04,
        0x01,
        0x93,
        0x93,
        0xD2,
        0xD2,
        0x00,
        0x00,
        0x00,
        0x07,
        0x09,
        0x01,
        0x94,
        0x94,
        0xD3,
        0xD3,
        0x00,
        0x00,
        0x00,
        0x01,
        0x0E,
        0x01,
        0x95,
        0x95,
        0xA5,
        0xA5,
        0x00,
        0x00,
        0x00,
        0x02,
        0x13,
        0x01,
        0x96,
        0x96,
        0xD0,
        0xD0,
        0x00,
        0x00,
        0x00,
        0x03,
        0x18,
        0x01,
        0x97,
        0x97,
        0xD1,
        0xD1,
        0x00,
        0x00,
        0x00,
        0x00,
        0x1D,
        0x01,
        0x98,
        0x98,
        0x7E,
        0x7E,
        0x00,
        0x00,
        0x00,
        0x00,
        0x22,
        0x01,
        0x99,
        0x99,
        0xAA,
        0xAA,
        0x00,
        0x00,
        0x02,
        0x00,
        0x36,
        0x06,
        0x73,
        0x9A,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0x31,
        0x01,
        0x9B,
        0x9B,
        0xDD,
        0xDD,
        0x00,
        0x00,
        0x0C,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0x9C,
        0xCF,
        0xCF,
        0x00,
        0x00,
        0x00,
        0x00,
        0x37,
        0x01,
        0x9D,
        0x9D,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0x38,
        0x01,
        0x9E,
        0x9E,
        0x20,
        0x20,
        0x00,
        0x00,
        0x02,
        0x00,
        0xAE,
        0x06,
        0x79,
        0x9F,
        0xD9,
        0xD9,
        0x00,
        0x00,
        0x00,
        0x00,
        0x3C,
        0x01,
        0xA0,
        0xA0,
        0xA0,
        0xA0,
        0x00,
        0x00,
        0x00,
        0x00,
        0x40,
        0x01,
        0xA1,
        0xA1,
        0xC1,
        0xC1,
        0x00,
        0x00,
        0x00,
        0x00,
        0x45,
        0x01,
        0xA2,
        0xA2,
        0xA2,
        0xA2,
        0x00,
        0x00,
        0x00,
        0x00,
        0x4A,
        0x01,
        0xA3,
        0xA3,
        0xA3,
        0xA3,
        0x00,
        0x00,
        0x00,
        0x00,
        0x4F,
        0x01,
        0xA4,
        0xA4,
        0xDB,
        0xDB,
        0x00,
        0x00,
        0x00,
        0x00,
        0x54,
        0x01,
        0xA5,
        0xA5,
        0xB4,
        0xB4,
        0x00,
        0x00,
        0x00,
        0x00,
        0x59,
        0x01,
        0xA6,
        0xA6,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0x5E,
        0x01,
        0xA7,
        0xA7,
        0xA4,
        0xA4,
        0x00,
        0x00,
        0x00,
        0x00,
        0x63,
        0x01,
        0xA8,
        0xA8,
        0xAC,
        0xAC,
        0x00,
        0x00,
        0x00,
        0x00,
        0x68,
        0x01,
        0xA9,
        0xA9,
        0xA9,
        0xA9,
        0x00,
        0x00,
        0x00,
        0x00,
        0x6D,
        0x01,
        0xAA,
        0xAA,
        0xBB,
        0xBB,
        0x00,
        0x00,
        0x00,
        0x00,
        0x72,
        0x01,
        0xAB,
        0xAB,
        0xC7,
        0xC7,
        0x00,
        0x00,
        0x00,
        0x00,
        0x77,
        0x01,
        0xAC,
        0xAC,
        0xC2,
        0xC2,
        0x00,
        0x00,
        0x00,
        0x00,
        0x7C,
        0x01,
        0xAD,
        0xAD,
        0x2D,
        0x2D,
        0x00,
        0x00,
        0x00,
        0x00,
        0x81,
        0x01,
        0xAE,
        0xAE,
        0xA8,
        0xA8,
        0x00,
        0x00,
        0x00,
        0x00,
        0x86,
        0x01,
        0xAF,
        0xAF,
        0xF8,
        0xF8,
        0x00,
        0x00,
        0x00,
        0x00,
        0x8B,
        0x01,
        0xB0,
        0xB0,
        0xA1,
        0xA1,
        0x00,
        0x00,
        0x00,
        0x00,
        0x90,
        0x01,
        0xB1,
        0xB1,
        0xB1,
        0xB1,
        0x00,
        0x00,
        0x00,
        0x00,
        0x95,
        0x01,
        0xB2,
        0xB2,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0x9A,
        0x01,
        0xB3,
        0xB3,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0x9F,
        0x01,
        0xB4,
        0xB4,
        0xAB,
        0xAB,
        0x00,
        0x00,
        0x00,
        0x00,
        0xA4,
        0x01,
        0xB5,
        0xB5,
        0xB5,
        0xB5,
        0x00,
        0x00,
        0x00,
        0x00,
        0xA9,
        0x01,
        0xB6,
        0xB6,
        0xA6,
        0xA6,
        0x00,
        0x00,
        0x00,
        0x00,
        0xAE,
        0x01,
        0xB7,
        0xB7,
        0xE1,
        0xE1,
        0x00,
        0x00,
        0x00,
        0x00,
        0xB3,
        0x01,
        0xB8,
        0xB8,
        0xFC,
        0xFC,
        0x00,
        0x00,
        0x00,
        0x00,
        0xB8,
        0x01,
        0xB9,
        0xB9,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xBD,
        0x01,
        0xBA,
        0xBA,
        0xBC,
        0xBC,
        0x00,
        0x00,
        0x00,
        0x00,
        0xC2,
        0x01,
        0xBB,
        0xBB,
        0xC8,
        0xC8,
        0x00,
        0x00,
        0x00,
        0x00,
        0xC7,
        0x01,
        0xBC,
        0xBC,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xCC,
        0x01,
        0xBD,
        0xBD,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xD1,
        0x01,
        0xBE,
        0xBE,
        0x20,
        0x20,
        0x00,
        0x00,
        0x00,
        0x00,
        0xD6,
        0x01,
        0xBF,
        0xBF,
        0xC0,
        0xC0,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xC0,
        0xCB,
        0xCB,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xC1,
        0xE7,
        0xE7,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xC2,
        0xE5,
        0xE5,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xC3,
        0xCC,
        0xCC,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xC4,
        0x80,
        0x80,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xC5,
        0x81,
        0x81,
        0x00,
        0x00,
        0x0C,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xC6,
        0xAE,
        0xAE,
        0x00,
        0x00,
        0x02,
        0x00,
        0xF6,
        0x04,
        0x63,
        0xC7,
        0x82,
        0x82,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xC8,
        0xE9,
        0xE9,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xC9,
        0x83,
        0x83,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xCA,
        0xE6,
        0xE6,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xCB,
        0xE8,
        0xE8,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xCC,
        0xED,
        0xED,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xCD,
        0xEA,
        0xEA,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xCE,
        0xEB,
        0xEB,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xCF,
        0xEC,
        0xEC,
        0x00,
        0x00,
        0x02,
        0x00,
        0x0A,
        0x05,
        0x64,
        0xD0,
        0x20,
        0x20,
        0x00,
        0x00,
        0x02,
        0x00,
        0xD2,
        0x05,
        0x6E,
        0xD1,
        0x84,
        0x84,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xD2,
        0xF1,
        0xF1,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xD3,
        0xEE,
        0xEE,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xD4,
        0xEF,
        0xEF,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xD5,
        0xCD,
        0xCD,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xD6,
        0x85,
        0x85,
        0x00,
        0x00,
        0x00,
        0x00,
        0xDB,
        0x01,
        0xD7,
        0xD7,
        0x20,
        0x20,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xD8,
        0xAF,
        0xAF,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xD9,
        0xF4,
        0xF4,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xDA,
        0xF2,
        0xF2,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xDB,
        0xF3,
        0xF3,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xDC,
        0x86,
        0x86,
        0x00,
        0x00,
        0x02,
        0x00,
        0xAE,
        0x06,
        0x79,
        0xDD,
        0x20,
        0x20,
        0x00,
        0x00,
        0x02,
        0x00,
        0x42,
        0x04,
        0xDE,
        0xDE,
        0x20,
        0x20,
        0x00,
        0x00,
        0x0C,
        0x00,
        0x36,
        0x06,
        0x73,
        0xDF,
        0xA7,
        0xA7,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xE0,
        0x88,
        0x88,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xE1,
        0x87,
        0x87,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xE2,
        0x89,
        0x89,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xE3,
        0x8B,
        0x8B,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xE4,
        0x8A,
        0x8A,
        0x00,
        0x00,
        0x02,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xE5,
        0x8C,
        0x8C,
        0x00,
        0x00,
        0x0C,
        0x00,
        0xCE,
        0x04,
        0x61,
        0xE6,
        0xBE,
        0xBE,
        0x00,
        0x00,
        0x02,
        0x00,
        0xF6,
        0x04,
        0x63,
        0xE7,
        0x8D,
        0x8D,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xE8,
        0x8F,
        0x8F,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xE9,
        0x8E,
        0x8E,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xEA,
        0x90,
        0x90,
        0x00,
        0x00,
        0x02,
        0x00,
        0x1E,
        0x05,
        0x65,
        0xEB,
        0x91,
        0x91,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xEC,
        0x93,
        0x93,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xED,
        0x92,
        0x92,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xEE,
        0x94,
        0x94,
        0x00,
        0x00,
        0x02,
        0x00,
        0x6E,
        0x05,
        0x69,
        0xEF,
        0x95,
        0x95,
        0x00,
        0x00,
        0x02,
        0x00,
        0x0A,
        0x05,
        0x6F,
        0xF0,
        0x20,
        0x20,
        0x00,
        0x00,
        0x02,
        0x00,
        0xD2,
        0x05,
        0x6E,
        0xF1,
        0x96,
        0x96,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xF2,
        0x98,
        0x98,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xF3,
        0x97,
        0x97,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xF4,
        0x99,
        0x99,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xF5,
        0x9B,
        0x9B,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xF6,
        0x9A,
        0x9A,
        0x00,
        0x00,
        0x00,
        0x00,
        0x66,
        0x00,
        0xF7,
        0xF7,
        0xD6,
        0xD6,
        0x00,
        0x00,
        0x02,
        0x00,
        0xE6,
        0x05,
        0x6F,
        0xF8,
        0xBF,
        0xBF,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xF9,
        0x9D,
        0x9D,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xFA,
        0x9C,
        0x9C,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xFB,
        0x9E,
        0x9E,
        0x00,
        0x00,
        0x02,
        0x00,
        0x5E,
        0x06,
        0x75,
        0xFC,
        0x9F,
        0x9F,
        0x00,
        0x00,
        0x02,
        0x00,
        0xAE,
        0x06,
        0x79,
        0xFD,
        0x20,
        0x20,
        0x00,
        0x00,
        0x02,
        0x00,
        0x4C,
        0x04,
        0xFE,
        0xFE,
        0x20,
        0x20,
        0x00,
        0x00,
        0x02,
        0x00,
        0xAE,
        0x06,
        0x79,
        0xFF,
        0xD8,
        0xD8,
        0x00,
        0x00,
    ]
)


def _encode_compressed_int(value: int) -> bytes:
    if value < 0:
        raise ValueError("Cannot encode negative value")
    if value == 0:
        return b"\x00"
    parts: list[int] = []
    while value > 0:
        parts.append(value & 0x7F)
        value >>= 7
    parts.reverse()
    for i in range(len(parts) - 1):
        parts[i] |= 0x80
    return bytes(parts)


def _encode_utf16le(s: str) -> bytes:
    return s.encode("utf-16-le")


@dataclass
class _Entry:
    name: str
    section: int
    offset: int
    length: int


def _extract_html_title(data: bytes) -> str:
    try:
        text = data.decode("latin-1")
    except (UnicodeDecodeError, LookupError):
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"<[^>]+>", "", title)
        title = re.sub(r"\s+", " ", title).strip()
        return title
    return ""


def _compute_url_hash(url: str) -> int:
    h = 0
    for c in url:
        o = ord(c)
        if c == "/":
            h = (43 * h + 44) & 0xFFFFFFFF
        elif o > ord("Z"):
            h = (43 * h + (o - 80)) & 0xFFFFFFFF
        else:
            h = (43 * h + (o - 48)) & 0xFFFFFFFF
    return h if h else 1


def _parse_hhc_topics(hhc_path: Path) -> list[tuple[int, int, str, str]]:
    """Parse a .hhc sitemap file, returning (depth, order, title, url) tuples."""
    if not hhc_path.exists():
        return []
    data = hhc_path.read_text(encoding="utf-8", errors="replace")

    entries: list[tuple[int, int, str, str]] = []
    depth = 0
    order = 0
    for tag_match in re.finditer(
        r"<(/?(?:UL|OBJECT)[^>]*)>",
        data,
        re.IGNORECASE,
    ):
        tag = tag_match.group(1)
        tag_upper = tag.upper()
        if tag_upper == "UL":
            depth += 1
        elif tag_upper == "/UL":
            depth -= 1
        elif "OBJECT" in tag_upper and "text/sitemap" in tag.lower():
            obj_end = data.find("</OBJECT>", tag_match.end())
            if obj_end < 0:
                continue
            body = data[tag_match.end() : obj_end]
            name = ""
            local = ""
            for param in re.finditer(
                r"<param\s+name\s*=\s*\"(\w+)\"\s+value\s*=\s*\"([^\"]*)\"",
                body,
                re.IGNORECASE,
            ):
                pname = param.group(1).lower()
                pval = param.group(2)
                if pname == "name":
                    name = pval
                elif pname == "local":
                    local = pval
            if local:
                entries.append((depth, order, name, local))
                order += 1

    return entries


class _StringsTable:
    """Builds the #STRINGS file with 0x1000-block alignment and dedup."""

    def __init__(self) -> None:
        self._data = bytearray(b"\x00")
        self._map: dict[str, int] = {}

    def add(self, s: str, dedup: bool = True) -> int:
        if not s:
            return 0
        if dedup and s in self._map:
            return self._map[s]

        encoded = s.encode("latin-1", errors="replace") + b"\x00"
        pos = len(self._data)

        next_block = (pos & ~0xFFF) + 0x1000
        if pos + len(encoded) > next_block:
            partial_len = next_block - pos
            self._data.extend(encoded[:partial_len])
            pos = next_block

        if dedup:
            self._map[s] = pos
        self._data.extend(encoded)
        return pos

    def get_offset(self, s: str) -> int:
        if not s:
            return 0
        return self._map.get(s, 0)

    def data(self) -> bytes:
        return bytes(self._data)


class _TopicTable:
    """Builds #TOPICS, #URLTBL, #URLSTR files.

    Mirrors hhc.exe behavior:
    - [FILES] topics are added first with in_contents=2 and deferred titles
    - .hhc entries UPDATE matching topics (flags -> 6, title resolved immediately)
    - .hhc entries with #anchors always create new topics (flags=4)
    - Deferred titles are resolved to #STRINGS offsets during finalize()
    """

    def __init__(self, strings: _StringsTable) -> None:
        self._strings = strings
        self._urlstr = bytearray(b"\x00")
        self._urlstr_block_pos = 1
        self._urlstr_block_num = 0
        self._topic_count = 0
        self._pending: list[list] = []
        self._url_to_idx: dict[str, int] = {}
        self._toc_offs: dict[int, int] = {}

    def add_topic(self, title: str, url: str, in_contents: int = 2) -> int:
        url_clean = url.replace("\\", "/")
        url_for_str = url_clean.lstrip("/")

        if in_contents & 0x04:
            str_val: int | str = (
                self._strings.add(title, dedup=False) if title else 0xFFFFFFFF
            )
        else:
            str_val = title

        urlstr_offset = self._add_urlstr(url_for_str)

        topic_idx = self._topic_count
        self._pending.append(
            [topic_idx, str_val, in_contents, url_for_str, urlstr_offset]
        )
        self._url_to_idx[url_for_str.lower()] = len(self._pending) - 1
        self._topic_count += 1
        return topic_idx

    def add_topic_with_str_offset(
        self, str_offset: int, url: str, in_contents: int
    ) -> int:
        url_clean = url.replace("\\", "/")
        url_for_str = url_clean.lstrip("/")
        urlstr_offset = self._add_urlstr(url_for_str)
        topic_idx = self._topic_count
        self._pending.append(
            [topic_idx, str_offset, in_contents, url_for_str, urlstr_offset]
        )
        self._url_to_idx[url_for_str.lower()] = len(self._pending) - 1
        self._topic_count += 1
        return topic_idx

    def topic_index_for_url(self, url: str) -> int | None:
        url_clean = url.replace("\\", "/").lstrip("/")
        return self._url_to_idx.get(url_clean.lower())

    def update_topic_from_toc(self, url: str, in_contents: int = 6) -> int | None:
        url_clean = url.replace("\\", "/").lstrip("/")
        idx = self._url_to_idx.get(url_clean.lower())
        if idx is None:
            return None
        entry = self._pending[idx]
        if isinstance(entry[1], str):
            entry[1] = (
                self._strings.add(entry[1], dedup=False) if entry[1] else 0xFFFFFFFF
            )
        entry[2] = in_contents
        return entry[0]

    def set_toc_off(self, topic_idx: int, toc_off: int) -> None:
        self._toc_offs[topic_idx] = toc_off

    def _add_urlstr(self, url: str) -> int:
        encoded = url.encode("ascii", errors="replace") + b"\x00"
        entry_size = 8 + len(encoded)

        if self._urlstr_block_pos + entry_size > 0x1000:
            pad = 0x1000 - self._urlstr_block_pos
            self._urlstr.extend(b"\x00" * pad)
            self._urlstr_block_num += 1
            self._urlstr_block_pos = 0

        offset = (self._urlstr_block_num << 12) | self._urlstr_block_pos

        self._urlstr.extend(struct.pack("<II", 0, 0))
        self._urlstr.extend(encoded)
        self._urlstr_block_pos += entry_size

        return offset

    def finalize(self) -> None:
        for entry in self._pending:
            if isinstance(entry[1], str):
                entry[1] = (
                    self._strings.add(entry[1], dedup=False) if entry[1] else 0xFFFFFFFF
                )

        urltbl_entries: list[tuple[int, int, int]] = []
        for topic_idx, _, _, url_for_str, urlstr_offset in self._pending:
            url_hash = _compute_url_hash(url_for_str)
            urltbl_entries.append((url_hash, topic_idx, urlstr_offset))

        urltbl_entries.sort(key=lambda e: e[0])

        urltbl_offset_map: dict[int, int] = {}
        urltbl = BytesIO()
        for url_hash, topic_idx, urlstr_offset in urltbl_entries:
            if urltbl.tell() % 0x1000 == 0xFFC:
                urltbl.write(struct.pack("<I", 0))
            urltbl_offset_map[topic_idx] = urltbl.tell()
            urltbl.write(struct.pack("<III", url_hash, topic_idx, urlstr_offset))

        topics = BytesIO()
        for topic_idx, str_offset, in_contents, _, _ in self._pending:
            urltbl_off = urltbl_offset_map[topic_idx]
            toc_off = self._toc_offs.get(topic_idx, 0)
            topics.write(
                struct.pack(
                    "<IIII", toc_off, str_offset, urltbl_off, (in_contents & 0xFFFF)
                )
            )

        self._topics_data = topics.getvalue()
        self._urltbl_data = urltbl.getvalue()

    @property
    def topic_count(self) -> int:
        return self._topic_count

    def topics_data(self) -> bytes:
        return self._topics_data

    def urltbl_data(self) -> bytes:
        return self._urltbl_data

    def urlstr_data(self) -> bytes:
        return bytes(self._urlstr)


ENGLISH_STOP_WORDS = [
    "into",
    "by",
    "in",
    "of",
    "at",
    "if",
    "no",
    "as",
    "on",
    "near",
    "or",
    "it",
    "is",
    "be",
    "was",
    "the",
    "to",
    "such",
    "and",
    "for",
    "a",
    "but",
    "their",
    "not",
    "that",
    "they",
    "are",
    "will",
    "then",
    "this",
    "these",
    "with",
    "there",
]

SUFFIX_SORT_DATA = bytes(
    [
        0xC6,
        0x61,
        0x65,
        0xE6,
        0x61,
        0x65,
        0xDF,
        0x73,
        0x73,
        0x8C,
        0x6F,
        0x65,
        0x9C,
        0x6F,
        0x65,
    ]
)


def _build_objinst(file_count: int) -> bytes:
    buf = BytesIO()
    buf.write(struct.pack("<I", 0x04000000))
    buf.write(struct.pack("<I", 2))

    entry1_payload = BytesIO()
    entry1_payload.write(struct.pack("<IHH", 0x4662DAAF, 0xD393, 0x11D0))
    entry1_payload.write(bytes([0x9A, 0x56, 0x00, 0xC0, 0x4F, 0xB6, 0x8B, 0xF7]))
    entry1_payload.write(struct.pack("<I", 0x04000000))
    entry1_payload.write(struct.pack("<I", len(SUFFIX_SORT_DATA)))
    entry1_payload.write(struct.pack("<II", 1252, 1033))
    entry1_payload.write(struct.pack("<II", 0, 0))
    entry1_payload.write(struct.pack("<II", 0x00145555, 0x00000A0F))
    entry1_payload.write(struct.pack("<H", 0x0100))
    entry1_payload.write(struct.pack("<I", 0x00030005))
    entry1_payload.write(b"\x00" * 24)
    entry1_payload.write(struct.pack("<H", 0))
    entry1_payload.write(OBJINST_CHAR_TABLE)
    entry1_payload.write(SUFFIX_SORT_DATA)

    stop_word_data = bytearray()
    for word in ENGLISH_STOP_WORDS:
        stop_word_data.extend(struct.pack("<H", len(word)))
        stop_word_data.extend(word.encode("ascii"))
    stop_word_data.extend(struct.pack("<H", 0))

    num_entries = len(ENGLISH_STOP_WORDS) + 1
    stop_data_len = len(stop_word_data) - 2
    header_len = 2 + 2 + 2 + num_entries
    entry1_payload.write(struct.pack("<BB", num_entries, num_entries))
    entry1_payload.write(struct.pack("<H", header_len))
    entry1_payload.write(struct.pack("<H", stop_data_len))
    entry1_payload.write(b"\x00" * num_entries)
    entry1_payload.write(stop_word_data)

    sub_entry = BytesIO()
    sub_entry.write(struct.pack("<IHH", 0x8FA0D5A8, 0xDEDF, 0x11D0))
    sub_entry.write(bytes([0x9A, 0x61, 0x00, 0xC0, 0x4F, 0xB6, 0x8B, 0xF7]))
    sub_entry.write(struct.pack("<I", 0x04000000))
    sub_entry.write(struct.pack("<III", 1, 1252, 1033))
    sub_entry.write(struct.pack("<I", 0))
    entry1_payload.write(sub_entry.getvalue())

    entry1_data = entry1_payload.getvalue()
    entry1_size = len(entry1_data)

    entry2_payload = BytesIO()
    entry2_payload.write(struct.pack("<IHH", 0x4662DAB0, 0xD393, 0x11D0))
    entry2_payload.write(bytes([0x9A, 0x56, 0x00, 0xC0, 0x4F, 0xB6, 0x8B, 0x66]))
    entry2_payload.write(struct.pack("<IIIII", file_count, 1252, 1033, 10031, 0))
    entry2_data = entry2_payload.getvalue()
    entry2_size = len(entry2_data)

    header_size = 24
    entry1_offset = header_size
    entry2_offset = entry1_offset + entry1_size

    buf.write(struct.pack("<II", entry1_offset, entry1_size))
    buf.write(struct.pack("<II", entry2_offset, entry2_size))

    buf.write(entry1_data)
    buf.write(entry2_data)

    return buf.getvalue()


def _build_idxhdr(
    topic_count: int, strings: _StringsTable, merge_files: list[str]
) -> bytes:
    buf = bytearray(b"\xff" * 4096)
    struct.pack_into("<4s", buf, 0, b"T#SM")
    struct.pack_into("<I", buf, 4, 1)
    struct.pack_into("<I", buf, 8, 1)
    struct.pack_into("<I", buf, 0x0C, topic_count)
    struct.pack_into("<I", buf, 0x10, 0)
    struct.pack_into("<I", buf, 0x18, 0)
    struct.pack_into("<I", buf, 0x1C, 1)
    struct.pack_into("<I", buf, 0x2C, 0x00801227)
    struct.pack_into("<I", buf, 0x40, 0)
    struct.pack_into("<I", buf, 0x44, 1)
    struct.pack_into("<I", buf, 0x48, len(merge_files))
    struct.pack_into("<I", buf, 0x4C, 1 if merge_files else 0)

    off = 0x50
    for mf in merge_files:
        struct.pack_into("<I", buf, off, strings.add(mf))
        off += 4

    # Zero-fill after header fields
    for i in range(off, 4096):
        buf[i] = 0

    return bytes(buf)


def _narrow_to_ansi(s: str) -> str:
    """Emulate hhc.exe's ANSI text handling for sitemap keywords.

    Numeric entities above 0xFF are truncated to a byte and reinterpreted
    in cp1252 (e.g. &#65533; becomes 0xFD, "ý").
    """
    out = []
    for ch in s:
        cp = ord(ch)
        if cp > 0xFF:
            try:
                ch = bytes([cp & 0xFF]).decode("cp1252")
            except UnicodeDecodeError:
                ch = chr(cp & 0xFF)
        out.append(ch)
    return "".join(out)


def _collect_index_keywords(
    hhk_path: Path, topics: _TopicTable
) -> list[tuple[str, list[int]]]:
    """Collect (keyword, topic_ids) pairs from a .hhk index sitemap.

    Duplicate keywords are merged (exact match) with their topics appended
    in document order, matching hhc.exe. Nested items become
    "Parent, Child" compound keywords.
    """
    sitemap = parse_sitemap(hhk_path)
    merged: dict[str, list[int]] = {}

    def walk(item: SiteMapItem, prefix: str) -> None:
        name = _narrow_to_ansi(item.name)
        keyword = f"{prefix}, {name}" if prefix else name
        if item.children:
            for child in item.children:
                walk(child, keyword)
            return
        if not keyword or not item.local:
            return
        tidx = topics.topic_index_for_url(item.local)
        if tidx is None:
            return
        merged.setdefault(keyword, []).append(tidx)

    for item in sitemap.items:
        walk(item, "")
    return list(merged.items())


def _filetime_now() -> int:
    return int((time.time() + 11644473600) * 10_000_000)


def _build_system_file(
    project: HHPProject,
    idxhdr_data: bytes | None = None,
    has_klinks: bool = False,
) -> bytes:
    buf = BytesIO()
    buf.write(struct.pack("<I", 3))

    def write_entry(code: int, data: bytes) -> None:
        buf.write(struct.pack("<HH", code, len(data)))
        buf.write(data)

    write_entry(10, struct.pack("<I", int(time.time())))
    write_entry(9, COMPILER_VERSION.encode("ascii") + b"\x00")

    fts_flag = 1 if project.full_text_search else 0
    entry4 = struct.pack(
        "<IIIIIQII",
        project.language,
        0,
        fts_flag,
        1 if has_klinks else 0,
        0,
        _filetime_now(),
        0,
        0,
    )
    write_entry(4, entry4)

    if project.default_topic:
        write_entry(2, project.default_topic.encode("ascii") + b"\x00")
    if project.title:
        write_entry(3, project.title.encode("ascii") + b"\x00")
    if project.default_font:
        write_entry(16, project.default_font.encode("ascii") + b"\x00")

    if project.compiled_file:
        basename = os.path.splitext(os.path.basename(project.compiled_file))[0].lower()
        write_entry(6, basename.encode("ascii") + b"\x00")

    if project.default_window:
        write_entry(5, project.default_window.encode("ascii") + b"\x00")

    write_entry(12, struct.pack("<I", 0))

    if idxhdr_data:
        write_entry(13, idxhdr_data)

    write_entry(15, struct.pack("<I", 0))

    return buf.getvalue()


def _build_windows_file(project: HHPProject, strings: _StringsTable) -> bytes:
    if not project.windows:
        return b""

    buf = BytesIO()
    buf.write(struct.pack("<II", len(project.windows), 196))

    for w in project.windows:
        entry = bytearray(196)

        def soff(s: str) -> int:
            return strings.add(s) if s else 0

        valid = 0
        if "win_properties" in w._provided:
            valid |= 0x0002
        if "style_flags" in w._provided:
            valid |= 0x0004
        if "extended_style_flags" in w._provided:
            valid |= 0x0008
        if "initial_pos" in w._provided:
            valid |= 0x0010
        if "nav_width" in w._provided:
            valid |= 0x0020
        if "show_state" in w._provided:
            valid |= 0x0040
        if "buttons" in w._provided:
            valid |= 0x0100
        if "not_expanded" in w._provided:
            valid |= 0x0200
        if "tab_pos" in w._provided:
            valid |= 0x0400

        struct.pack_into("<I", entry, 0, 196)
        struct.pack_into("<I", entry, 8, soff(w.name))
        struct.pack_into("<I", entry, 12, valid)
        struct.pack_into("<I", entry, 16, w.win_properties)
        struct.pack_into("<I", entry, 20, soff(w.title))
        struct.pack_into("<I", entry, 24, w.style_flags)
        struct.pack_into("<I", entry, 28, w.extended_style_flags)
        pos = w.initial_pos
        struct.pack_into(
            "<iiii",
            entry,
            32,
            pos[0] if len(pos) > 0 else 0,
            pos[1] if len(pos) > 1 else 0,
            pos[2] if len(pos) > 2 else 0,
            pos[3] if len(pos) > 3 else 0,
        )
        struct.pack_into("<I", entry, 48, w.show_state)
        # offset 72: unknown (0), offset 76: iNavWidth
        struct.pack_into("<I", entry, 76, w.nav_width)
        # offset 80-92: rcHTML (zeros)
        struct.pack_into("<I", entry, 96, soff(w.toc_file))
        struct.pack_into("<I", entry, 100, soff(w.index_file))
        struct.pack_into("<I", entry, 104, soff(w.default_file))
        struct.pack_into("<I", entry, 108, soff(w.home_file))
        struct.pack_into("<I", entry, 112, w.buttons)
        struct.pack_into("<I", entry, 116, w.not_expanded)
        struct.pack_into("<I", entry, 120, w.cur_nav_type)
        struct.pack_into("<I", entry, 124, w.tab_pos)
        # offset 128: idNotify, 132-152: tabOrder, 156: cHistory
        struct.pack_into("<I", entry, 160, soff(w.jump1_text))
        struct.pack_into("<I", entry, 164, soff(w.jump2_text))
        struct.pack_into("<I", entry, 168, soff(w.jump1_url))
        struct.pack_into("<I", entry, 172, soff(w.jump2_url))

        buf.write(bytes(entry))

    return buf.getvalue()


def _build_namelist() -> bytes:
    buf = BytesIO()
    names = ["Uncompressed", "MSCompressed"]
    buf.write(struct.pack("<H", 0))
    buf.write(struct.pack("<H", len(names)))
    for name in names:
        encoded = _encode_utf16le(name)
        buf.write(struct.pack("<H", len(name)))
        buf.write(encoded)
        buf.write(b"\x00\x00")
    result = bytearray(buf.getvalue())
    struct.pack_into("<H", result, 0, len(result) // 2)
    return bytes(result)


def _build_control_data(
    window_size: int = LZX_RESET_INTERVAL, reset_interval: int = LZX_RESET_INTERVAL
) -> bytes:
    return struct.pack("<I4sIIIII", 6, b"LZXC", 2, reset_interval, window_size, 1, 0)


def _build_reset_table(
    frame_positions: list[int], uncompressed_len: int, compressed_len: int
) -> bytes:
    buf = BytesIO()
    buf.write(struct.pack("<I", 2))
    buf.write(struct.pack("<I", len(frame_positions)))
    buf.write(struct.pack("<I", 8))
    buf.write(struct.pack("<I", 0x28))
    buf.write(struct.pack("<Q", uncompressed_len))
    buf.write(struct.pack("<Q", compressed_len))
    buf.write(struct.pack("<Q", FRAME_SIZE))
    for pos in frame_positions:
        buf.write(struct.pack("<Q", pos))
    return buf.getvalue()


def _build_transform_list() -> bytes:
    return "{7FC28940-9D31-11D0".encode("utf-16-le")


def _build_directory_entry(name: str, section: int, offset: int, length: int) -> bytes:
    name_bytes = name.encode("utf-8")
    return (
        _encode_compressed_int(len(name_bytes))
        + name_bytes
        + _encode_compressed_int(section)
        + _encode_compressed_int(offset)
        + _encode_compressed_int(length)
    )


def _build_pmgl_chunk(entries: list[bytes], prev_idx: int, next_idx: int) -> bytes:
    data = bytearray(CHUNK_SIZE)
    data[0:4] = PMGL_SIGNATURE
    pos = 0x14

    entry_offsets: list[int] = []
    for entry in entries:
        entry_offsets.append(pos)
        data[pos : pos + len(entry)] = entry
        pos += len(entry)

    # The quickref area is mandatory: ITSS derives the number of refs from
    # the entry count and density, so the packer must have reserved room.
    n = len(entries)
    n_refs = (n - 1) // QUICKREF_DENSITY
    if pos > CHUNK_SIZE - 2 - 2 * n_refs:
        raise ValueError("PMGL chunk overflow: quickref area would be clobbered")

    free_space = CHUNK_SIZE - pos

    struct.pack_into("<I", data, 4, free_space)
    struct.pack_into("<I", data, 8, 0)
    struct.pack_into("<i", data, 12, prev_idx)
    struct.pack_into("<i", data, 16, next_idx)

    struct.pack_into("<H", data, CHUNK_SIZE - 2, n)
    for j in range(1, n_refs + 1):
        struct.pack_into(
            "<H",
            data,
            CHUNK_SIZE - 2 - 2 * j,
            entry_offsets[j * QUICKREF_DENSITY] - 0x14,
        )

    return bytes(data)


def _build_pmgi_chunk(entries: list[tuple[str, int]]) -> bytes:
    data = bytearray(CHUNK_SIZE)
    data[0:4] = PMGI_SIGNATURE
    pos = 8

    entry_offsets: list[int] = []
    for name, chunk_idx in entries:
        name_bytes = name.encode("utf-8")
        enc = (
            _encode_compressed_int(len(name_bytes))
            + name_bytes
            + _encode_compressed_int(chunk_idx)
        )
        entry_offsets.append(pos)
        data[pos : pos + len(enc)] = enc
        pos += len(enc)

    n = len(entries)
    n_refs = (n - 1) // QUICKREF_DENSITY
    if pos > CHUNK_SIZE - 2 - 2 * n_refs:
        raise ValueError("PMGI chunk overflow: quickref area would be clobbered")

    free_space = CHUNK_SIZE - pos
    struct.pack_into("<I", data, 4, free_space)

    struct.pack_into("<H", data, CHUNK_SIZE - 2, n)
    for j in range(1, n_refs + 1):
        struct.pack_into(
            "<H",
            data,
            CHUNK_SIZE - 2 - 2 * j,
            entry_offsets[j * QUICKREF_DENSITY] - 8,
        )

    return bytes(data)


def _collect_directories(file_paths: list[str]) -> list[str]:
    dirs: set[str] = set()
    for path in file_paths:
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        parts = path.split("/")
        for i in range(2, len(parts)):
            d = "/".join(parts[:i]) + "/"
            dirs.add(d)
    return sorted(dirs)


def compile_chm(
    project: HHPProject,
    output_path: str | Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Path:
    if output_path is None:
        output_path = project.base_dir / project.compiled_file
    output_path = Path(output_path)

    strings = _StringsTable()
    topics = _TopicTable(strings)

    content_files: list[tuple[str, bytes]] = []
    content_set: set[str] = set()
    seen_urls: set[str] = set()

    hhc_entries: list[tuple[int, int, str, str]] = []
    if project.contents_file:
        hhc_path = project.resolve_path(project.contents_file)
        hhc_entries = _parse_hhc_topics(hhc_path)

    for rel_path in project.files:
        abs_path = project.resolve_path(rel_path)
        canon = "/" + rel_path.replace("\\", "/")
        if abs_path.exists():
            content_files.append((canon, abs_path.read_bytes()))
            content_set.add(canon)
        elif on_progress:
            on_progress(f"Warning: file not found: {abs_path}")

    if project.contents_file:
        hhc_abs = project.resolve_path(project.contents_file)
        if hhc_abs.exists():
            canon = "/" + project.contents_file.replace("\\", "/")
            if canon not in content_set:
                content_files.append((canon, hhc_abs.read_bytes()))
                content_set.add(canon)

    if project.index_file:
        hhk_abs = project.resolve_path(project.index_file)
        if hhk_abs.exists():
            canon = "/" + project.index_file.replace("\\", "/")
            if canon not in content_set:
                content_files.append((canon, hhk_abs.read_bytes()))
                content_set.add(canon)

    # Scan for files referenced from HTML content but not in [FILES]
    referenced_files: set[str] = set()
    for _, data in content_files:
        for m in re.finditer(
            rb'(?:src|href)\s*=\s*["\']([^"\'#?]+)', data, re.IGNORECASE
        ):
            ref = m.group(1).decode("ascii", errors="replace").replace("\\", "/")
            if not ref.startswith(("http://", "https://", "mailto:", "javascript:")):
                referenced_files.add(ref)

    for ref in sorted(referenced_files):
        canon = "/" + ref.lstrip("/")
        if canon in content_set:
            continue
        abs_path = project.resolve_path(ref)
        if abs_path.exists() and abs_path.is_file():
            content_files.append((canon, abs_path.read_bytes()))
            content_set.add(canon)

    if on_progress:
        on_progress(f"Compiling {len(content_files)} files...")

    # Build windows FIRST so window strings get added to #STRINGS before topic titles
    windows_data = _build_windows_file(project, strings)

    # Topic entries for HTML files from [FILES] (in_contents=2)
    fts_html_files: list[tuple[int, bytes]] = []
    for name, data in content_files:
        url_key = name.lstrip("/")
        if url_key in seen_urls:
            continue
        fname_lower = os.path.basename(name).lower()
        if ".ht" in fname_lower:
            title = _extract_html_title(data)
            tidx = topics.add_topic(title, name, in_contents=2)
            seen_urls.add(url_key)
            fts_html_files.append((tidx, data))

    # TOC and index files as topics (before .hhc processing, matching hhc.exe order)
    if project.contents_file:
        url_key = project.contents_file.replace("\\", "/")
        if url_key not in seen_urls:
            topics.add_topic("", project.contents_file, in_contents=2)
    if project.index_file:
        url_key = project.index_file.replace("\\", "/")
        if url_key not in seen_urls:
            topics.add_topic("", project.index_file, in_contents=2)

    # Determine which .hhc entries have children (are folders) from DFS order
    hhc_has_children: dict[int, bool] = {}
    for i, (depth, order, title, url) in enumerate(hhc_entries):
        hhc_has_children[order] = (
            i + 1 < len(hhc_entries) and hhc_entries[i + 1][0] > depth
        )

    # Pass 1 (BFS): resolve strings and update non-anchor topics
    bfs_entries = sorted(hhc_entries, key=lambda e: (e[0], e[1]))
    anchor_str_offsets: dict[int, int] = {}
    for depth, order, title, url in bfs_entries:
        url_canon = url.replace("\\", "/")
        if "#" in url_canon:
            str_off = strings.add(title, dedup=False) if title else 0xFFFFFFFF
            anchor_str_offsets[order] = str_off
        else:
            tidx = topics.update_topic_from_toc(url_canon, in_contents=6)
            if tidx is None:
                topics.add_topic(title, url_canon, in_contents=6)

    # Pass 2 (DFS): create anchor topics in document order
    dfs_anchor_topic_map: dict[int, int] = {}
    for depth, order, title, url in hhc_entries:
        url_canon = url.replace("\\", "/")
        if "#" in url_canon:
            str_off = anchor_str_offsets[order]
            tidx = topics.add_topic_with_str_offset(str_off, url_canon, in_contents=4)
            dfs_anchor_topic_map[order] = tidx

    # Build BFS topic index list for tocOff computation
    toc_topic_indices: list[int] = []
    for depth, order, title, url in bfs_entries:
        url_canon = url.replace("\\", "/")
        if "#" in url_canon:
            toc_topic_indices.append(dfs_anchor_topic_map[order])
        else:
            url_clean = url_canon.replace("\\", "/").lstrip("/")
            tidx = topics._url_to_idx.get(url_clean.lower())
            if tidx is not None:
                toc_topic_indices.append(topics._pending[tidx][0])
            else:
                toc_topic_indices.append(0)

    # Compute tocOff for each TOC entry using BFS block-packing algorithm
    block_num = 1
    pos_in_block = 0
    for i, (depth, order, title, url) in enumerate(bfs_entries):
        is_folder = hhc_has_children.get(order, False)
        entry_size = 28 if is_folder else 20
        if pos_in_block + entry_size > 4096:
            block_num += 1
            pos_in_block = 0
        toc_off = block_num * 4096 + pos_in_block
        topics.set_toc_off(toc_topic_indices[i], toc_off)
        pos_in_block += entry_size

    topics.finalize()

    # -- Binary keyword index ($WWKeywordLinks) from the .hhk --
    keyword_links: list[tuple[str, bytes]] = []
    if project.index_file:
        hhk_abs = project.resolve_path(project.index_file)
        if hhk_abs.exists():
            kw_pairs = _collect_index_keywords(hhk_abs, topics)
            if kw_pairs:
                btree_data, klinks_data, klinks_map, _ = build_keyword_links(
                    kw_pairs, locale_id=project.language & 0xFFFF
                )
                keyword_links = [
                    ("/$WWKeywordLinks/BTree", btree_data),
                    ("/$WWKeywordLinks/Data", klinks_data),
                    ("/$WWKeywordLinks/Map", klinks_map),
                ]

    idxhdr_data = _build_idxhdr(topics.topic_count, strings, project.merge_files)
    system_data = _build_system_file(
        project, idxhdr_data, has_klinks=bool(keyword_links)
    )

    # -- Build section 1 (compressed content + metadata) --
    all_sec1_data = bytearray()
    sec1_entries: list[_Entry] = []

    for name, data in content_files:
        offset = len(all_sec1_data)
        all_sec1_data.extend(data)
        sec1_entries.append(_Entry(name, 1, offset, len(data)))

    sec1_meta_pre: list[tuple[str, bytes]] = [
        ("/#TOPICS", topics.topics_data()),
        ("/#URLSTR", topics.urlstr_data()),
        ("/#URLTBL", topics.urltbl_data()),
        ("/#WINDOWS", windows_data),
        ("/#IDXHDR", idxhdr_data),
        ("/#STRINGS", strings.data()),
    ]

    fiftimain_data = b""
    if project.full_text_search and fts_html_files:
        stop_set = set(ENGLISH_STOP_WORDS)
        fiftimain_data = build_fiftimain(
            fts_html_files,
            stop_set,
            codepage=1252,
            locale_id=project.language & 0xFFFF,
        )
        if on_progress:
            on_progress(f"Built FTS index: {len(fiftimain_data)} bytes")

    klinks_property = PROPERTY_DATA if keyword_links else struct.pack("<I", 0)
    sec1_meta_post: list[tuple[str, bytes]] = [
        ("/$FIftiMain", fiftimain_data),
        *keyword_links,
        ("/$WWKeywordLinks/Property", klinks_property),
        ("/$WWAssociativeLinks/Property", struct.pack("<I", 0)),
    ]

    # hhc.exe's $OBJINST file count does not include BTree/Data/Map.
    keyword_link_names = {name for name, _ in keyword_links}
    file_count = len(content_files)
    for _, d in sec1_meta_pre:
        if d:
            file_count += 1
    for name, d in sec1_meta_post:
        if d and name not in keyword_link_names:
            file_count += 1
    objinst_data = _build_objinst(file_count)

    sec1_meta = sec1_meta_pre + [("/$OBJINST", objinst_data)] + sec1_meta_post

    for name, data in sec1_meta:
        if data:
            offset = len(all_sec1_data)
            all_sec1_data.extend(data)
            sec1_entries.append(_Entry(name, 1, offset, len(data)))

    pad = (-len(all_sec1_data)) % FRAME_SIZE
    if pad:
        all_sec1_data.extend(b"\x00" * pad)

    if on_progress:
        on_progress(f"Compressing {len(all_sec1_data)} bytes with LZX...")

    compressed_data, frame_positions, total_uncomp = lzx_compress(
        bytes(all_sec1_data), LZX_WINDOW_BITS, LZX_RESET_INTERVAL
    )

    # -- Build section 0 (uncompressed) --
    sec0_files: list[tuple[str, bytes]] = []
    sec0_files.append(("/#SYSTEM", system_data))
    sec0_files.append(("/#ITBITS", b""))

    namelist_data = _build_namelist()
    control_data = _build_control_data()
    reset_table = _build_reset_table(
        frame_positions, total_uncomp, len(compressed_data)
    )
    span_info = struct.pack("<Q", total_uncomp)
    transform_list = _build_transform_list()

    T = "::DataSpace/Storage/MSCompressed/Transform"
    TG = f"{T}/{TRANSFORM_GUID.decode('ascii')}/InstanceData"

    ds_files: list[tuple[str, bytes]] = [
        ("::DataSpace/NameList", namelist_data),
        ("::DataSpace/Storage/MSCompressed/ControlData", control_data),
        ("::DataSpace/Storage/MSCompressed/SpanInfo", span_info),
        (f"{T}/List", transform_list),
        (f"{TG}/", b""),
        (f"{TG}/ResetTable", reset_table),
        ("::DataSpace/Storage/MSCompressed/Content", compressed_data),
    ]

    all_sec0_data = bytearray()
    sec0_entries: list[_Entry] = []
    for name, data in sec0_files:
        offset = len(all_sec0_data)
        all_sec0_data.extend(data)
        sec0_entries.append(_Entry(name, 0, offset, len(data)))

    for name, data in ds_files:
        offset = len(all_sec0_data)
        all_sec0_data.extend(data)
        sec0_entries.append(_Entry(name, 0, offset, len(data)))

    # -- Build directory entries --
    all_file_paths = [e.name for e in sec1_entries]
    directory_paths = _collect_directories(all_file_paths)

    dir_entries: list[_Entry] = []
    for d in directory_paths:
        dir_entries.append(_Entry(d, 0, 0, 0))
    for d in ("/$WWKeywordLinks/", "/$WWAssociativeLinks/"):
        if d not in directory_paths:
            dir_entries.append(_Entry(d, 0, 0, 0))

    all_entries = sec0_entries + sec1_entries + dir_entries
    all_entries.append(_Entry("/", 0, 0, 0))
    all_entries.sort(key=lambda e: e.name.lower())

    raw_entries: list[bytes] = []
    for entry in all_entries:
        raw_entries.append(
            _build_directory_entry(
                entry.name, entry.section, entry.offset, entry.length
            )
        )

    pmgl_chunks: list[bytes] = []
    pmgi_entries: list[tuple[str, int]] = []
    chunk_entries: list[bytes] = []
    entry_bytes_used = 0
    first_entry_name = ""

    for i, raw in enumerate(raw_entries):
        # Header + entries + count word + quickref slots must fit in the chunk.
        n_after = len(chunk_entries) + 1
        qr_area = 2 + 2 * ((n_after - 1) // QUICKREF_DENSITY)
        if chunk_entries and 0x14 + entry_bytes_used + len(raw) + qr_area > CHUNK_SIZE:
            chunk_idx = len(pmgl_chunks)
            pmgl_chunks.append(
                _build_pmgl_chunk(
                    chunk_entries, chunk_idx - 1 if chunk_idx > 0 else -1, -1
                )
            )
            pmgi_entries.append((first_entry_name, chunk_idx))
            chunk_entries = []
            entry_bytes_used = 0
            first_entry_name = ""

        if not chunk_entries:
            first_entry_name = all_entries[i].name
        chunk_entries.append(raw)
        entry_bytes_used += len(raw)

    if chunk_entries:
        chunk_idx = len(pmgl_chunks)
        pmgl_chunks.append(
            _build_pmgl_chunk(chunk_entries, chunk_idx - 1 if chunk_idx > 0 else -1, -1)
        )
        pmgi_entries.append((first_entry_name, chunk_idx))

    for i in range(len(pmgl_chunks) - 1):
        chunk = bytearray(pmgl_chunks[i])
        struct.pack_into("<i", chunk, 16, i + 1)
        pmgl_chunks[i] = bytes(chunk)

    num_pmgl = len(pmgl_chunks)
    pmgi_chunk_data: list[bytes] = []
    index_root = -1
    index_depth = 1

    if num_pmgl > 1:
        pmgi_chunk_data.append(_build_pmgi_chunk(pmgi_entries))
        index_root = num_pmgl
        index_depth = 2

    total_chunks = num_pmgl + len(pmgi_chunk_data)

    itsf_header_len = 0x60
    sect0_header = struct.pack("<IIQI", 0x01FE, 0, 0, 0)
    sect0_header += b"\x00" * 4
    sect0_len = 0x18

    itsp_header_len = 0x54
    dir_header = struct.pack(
        "<4siiIiiiiiiiII",
        ITSP_SIGNATURE,
        1,
        itsp_header_len,
        0x0A,
        CHUNK_SIZE,
        2,
        index_depth,
        index_root,
        0,
        num_pmgl - 1,
        -1,
        total_chunks,
        project.language,
    )
    dir_header += ITSP_GUID
    dir_header += struct.pack("<Iiii", itsp_header_len, -1, -1, -1)

    dir_total_len = itsp_header_len + total_chunks * CHUNK_SIZE
    sect0_offset = itsf_header_len
    dir_offset = sect0_offset + sect0_len
    data_offset = dir_offset + dir_total_len
    total_file_len = data_offset + len(all_sec0_data)

    sect0_data_real = struct.pack("<IIQI", 0x01FE, 0, total_file_len, 0)
    sect0_data_real += b"\x00" * 4
    sect0_data_real = sect0_data_real[:sect0_len]

    itsf_header = struct.pack(
        "<4sIIII",
        ITSF_SIGNATURE,
        3,
        itsf_header_len,
        1,
        int(time.time()) & 0xFFFFFFFF,
    )
    itsf_header += struct.pack("<I", project.language)
    itsf_header += GUID1
    itsf_header += GUID2
    itsf_header += struct.pack("<QQ", sect0_offset, sect0_len)
    itsf_header += struct.pack("<QQ", dir_offset, dir_total_len)
    itsf_header += struct.pack("<Q", data_offset)

    out = BytesIO()
    out.write(itsf_header)

    assert out.tell() == sect0_offset
    out.write(sect0_data_real)

    assert out.tell() == dir_offset
    out.write(dir_header)
    for chunk in pmgl_chunks:
        out.write(chunk)
    for chunk in pmgi_chunk_data:
        out.write(chunk)

    assert out.tell() == data_offset
    out.write(all_sec0_data)

    assert out.tell() == total_file_len

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(out.getvalue())

    if on_progress:
        on_progress(f"Created {output_path} ({total_file_len} bytes)")

    return output_path
