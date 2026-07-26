"""Parse HTML Help sitemap (.hhc / .hhk) files."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


@dataclass
class SiteMapItem:
    name: str = ""
    local: str = ""
    image_number: int = -1
    children: list[SiteMapItem] = field(default_factory=list)


@dataclass
class SiteMap:
    items: list[SiteMapItem] = field(default_factory=list)
    image_type: str = ""
    window_styles: str = ""


class _SiteMapParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.result = SiteMap()
        self._container_stack: list[list[SiteMapItem]] = [self.result.items]
        self._current_item: SiteMapItem | None = None
        self._in_site_properties = False
        self._last_item: SiteMapItem | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "ul":
            if self._last_item is not None:
                self._container_stack.append(self._last_item.children)
            else:
                self._container_stack.append(self.result.items)
        elif tag == "object":
            obj_type = dict(attrs).get("type", "")
            if obj_type == "text/sitemap":
                self._current_item = SiteMapItem()
            elif obj_type == "text/site properties":
                self._in_site_properties = True
        elif tag == "param":
            attr_dict = {k.lower(): v for k, v in attrs if v is not None}
            name = attr_dict.get("name", "").lower()
            value = attr_dict.get("value", "")
            if self._in_site_properties:
                if name == "imagetype":
                    self.result.image_type = value
                elif name == "window styles":
                    self.result.window_styles = value
            elif self._current_item is not None:
                if name == "name":
                    self._current_item.name = value
                elif name == "local":
                    self._current_item.local = value
                elif name == "imagenumber":
                    try:
                        self._current_item.image_number = int(value)
                    except ValueError:
                        pass

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "ul":
            if len(self._container_stack) > 1:
                self._container_stack.pop()
        elif tag == "object":
            if self._in_site_properties:
                self._in_site_properties = False
            elif self._current_item is not None:
                item = self._current_item
                self._current_item = None
                self._container_stack[-1].append(item)
                self._last_item = item


def parse_sitemap(path: str | Path) -> SiteMap:
    # hhc.exe reads sitemap files in the ANSI code page, not UTF-8.
    with open(path, "r", encoding="cp1252", errors="replace") as f:
        content = f.read()
    parser = _SiteMapParser()
    parser.feed(content)
    return parser.result
