"""Parse HTML Help project (.hhp) files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split_window_values(s: str) -> list[str]:
    """Split comma-separated values, keeping bracket groups intact."""
    result: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in s:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            result.append("".join(current))
            current = []
        else:
            current.append(ch)
    result.append("".join(current))
    return result


@dataclass
class WindowDef:
    name: str = ""
    title: str = ""
    toc_file: str = ""
    index_file: str = ""
    default_file: str = ""
    home_file: str = ""
    jump1_url: str = ""
    jump1_text: str = ""
    jump2_url: str = ""
    jump2_text: str = ""
    win_properties: int = 0
    nav_width: int = 0
    buttons: int = 0
    initial_pos: tuple[int, int, int, int] = (0, 0, 0, 0)
    style_flags: int = 0
    extended_style_flags: int = 0
    show_state: int = 0
    not_expanded: int = 0
    cur_nav_type: int = 0
    default_nav_pane: int = 0
    tab_pos: int = 0
    _provided: set = field(default_factory=set)

    @classmethod
    def parse(cls, line: str) -> WindowDef:
        w = cls()
        parts = line.split("=", 1)
        if len(parts) < 2:
            return w
        w.name = parts[0].strip().strip('"')
        values = _split_window_values(parts[1])

        def get(i: int, default: str = "") -> str:
            if i < len(values):
                return values[i].strip().strip('"')
            return default

        def get_int(i: int, default: int = 0) -> int:
            v = get(i)
            if not v:
                return default
            try:
                if v.startswith(("0x", "0X")):
                    return int(v, 16)
                return int(v)
            except ValueError:
                return default

        def set_field(name: str, val):
            setattr(w, name, val)
            w._provided.add(name)

        w.title = get(0)
        w.toc_file = get(1)
        w.index_file = get(2)
        w.default_file = get(3)
        w.home_file = get(4)
        w.jump1_url = get(5)
        w.jump1_text = get(6)
        w.jump2_url = get(7)
        w.jump2_text = get(8)

        if get(9):
            set_field("win_properties", get_int(9))
        if get(10):
            set_field("nav_width", get_int(10))
        if get(11):
            set_field("buttons", get_int(11))

        pos_str = get(12)
        if pos_str.startswith("["):
            pos_parts = pos_str.strip("[]").split(",")
            try:
                parts = [int(p.strip()) for p in pos_parts[:4]]
                w.initial_pos = (parts[0], parts[1], parts[2], parts[3])
                w._provided.add("initial_pos")
            except ValueError:
                pass

        if get(13):
            set_field("style_flags", get_int(13))
        if get(14):
            set_field("extended_style_flags", get_int(14))
        if get(15):
            set_field("show_state", get_int(15))
        if get(16):
            set_field("not_expanded", get_int(16))
        if get(17):
            set_field("cur_nav_type", get_int(17))
        if get(18):
            set_field("default_nav_pane", get_int(18))
        if get(19) or (len(values) > 19 and values[19].strip() != ""):
            set_field("tab_pos", get_int(19))

        return w


@dataclass
class HHPProject:
    base_dir: Path = field(default_factory=lambda: Path("."))

    # [OPTIONS]
    compiled_file: str = ""
    contents_file: str = ""
    index_file: str = ""
    default_topic: str = ""
    title: str = ""
    language: int = 0x0409
    default_window: str = ""
    full_text_search: bool = True
    binary_toc: bool = False
    binary_index: bool = False
    display_progress: bool = True
    default_font: str = ""
    stop_list_file: str = ""

    # [WINDOWS]
    windows: list[WindowDef] = field(default_factory=list)

    # [FILES]
    files: list[str] = field(default_factory=list)

    # [MERGE FILES]
    merge_files: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, hhp_path: str | Path) -> HHPProject:
        hhp_path = Path(hhp_path)
        project = cls(base_dir=hhp_path.parent)

        with open(hhp_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        section = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue

            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].upper()
                continue

            if section == "OPTIONS":
                _parse_option(project, line)
            elif section == "WINDOWS":
                project.windows.append(WindowDef.parse(line))
            elif section == "FILES":
                project.files.append(line)
            elif section == "MERGE FILES":
                project.merge_files.append(line)

        return project

    def resolve_path(self, relative: str) -> Path:
        return self.base_dir / relative.replace("\\", os.sep).replace("/", os.sep)


def _parse_option(project: HHPProject, line: str) -> None:
    eq = line.find("=")
    if eq < 0:
        return
    key = line[:eq].strip()
    value = line[eq + 1 :].strip()

    key_lower = key.lower()
    if key_lower == "compiled file":
        project.compiled_file = value
    elif key_lower == "contents file":
        project.contents_file = value
    elif key_lower == "index file":
        project.index_file = value
    elif key_lower == "default topic":
        project.default_topic = value
    elif key_lower == "title":
        project.title = value
    elif key_lower == "language":
        try:
            project.language = int(value, 0)
        except ValueError:
            pass
    elif key_lower == "default window":
        project.default_window = value
    elif key_lower == "full-text search":
        project.full_text_search = value.lower() == "yes"
    elif key_lower == "binary toc":
        project.binary_toc = value.lower() == "yes"
    elif key_lower == "binary index":
        project.binary_index = value.lower() == "yes"
    elif key_lower == "display compile progress":
        project.display_progress = value.lower() == "yes"
    elif key_lower == "default font":
        project.default_font = value
    elif key_lower == "full text search stop list file":
        project.stop_list_file = value
