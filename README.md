# pyhhc

Open-source Python reimplementation of Microsoft's `hhc.exe` (HTML Help Compiler). Compiles `.hhp` projects into `.chm` files with no external dependencies.

## Install

```
pip install pyhhc
```

## Usage

### Command line

```
pyhhc project.hhp
```

### Library

```python
from pyhhc import HHPProject, compile_chm

project = HHPProject.parse("project.hhp")
compile_chm(project)
```

## Features

- Full CHM container format (ITSF/ITSP headers, PMGL/PMGI directory)
- LZX compression
- Table of contents and index
- Full-text search index (`$FIftiMain`)
- All internal metadata files (`#SYSTEM`, `#STRINGS`, `#WINDOWS`, `#TOPICS`, `#URLTBL`, `#URLSTR`, `#IDXHDR`, `$OBJINST`)

## License

BSL-1.0
