"""Command-line interface for pyhhc."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .chm import compile_chm
from .project import HHPProject


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: pyhhc <project.hhp>")
        print("Compiles an HTML Help project (.hhp) into a .chm file.")
        return 1

    hhp_path = Path(sys.argv[1])
    if not hhp_path.exists():
        print(f"Error: file not found: {hhp_path}")
        return 1

    print("pyhhc - HTML Help Compiler")
    print(f"Compiling {hhp_path}...")

    start = time.time()

    project = HHPProject.parse(hhp_path)
    if not project.compiled_file:
        print("Error: no 'Compiled file' specified in [OPTIONS]")
        return 1

    def progress(msg: str) -> None:
        print(f"  {msg}")

    output = compile_chm(project, on_progress=progress)
    elapsed = time.time() - start

    print(f"Created {output} in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
