"""pyhhc - Open-source Python reimplementation of Microsoft's HTML Help Compiler."""

from .chm import compile_chm
from .project import HHPProject

__all__ = ["HHPProject", "compile_chm"]
