"""Validate ZIP members before bounded extraction."""

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .input_rows import MAX_FILE_BYTES, MAX_INPUT_BYTES

MAX_MEMBERS = 500
MAX_EXPANSION_RATIO = 1000


def safe_extract(path: Path, destination: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError("Archive exceeds member limit")
        names = set()
        total = 0
        for member in members:
            name = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in member.filename
                or str(name) in names
                or len(name.parts) > 2
                or stat.S_ISLNK(mode)
                or (
                    stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                )
            ):
                raise ValueError("Unsafe or duplicate archive member")
            names.add(str(name))
            total += member.file_size
            if (
                member.file_size > MAX_FILE_BYTES
                or total > MAX_INPUT_BYTES
                or member.file_size > max(1, member.compress_size) * MAX_EXPANSION_RATIO
            ):
                raise ValueError("Archive exceeds expansion limit")
        for member in members:
            target = destination / member.filename
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=65536)
