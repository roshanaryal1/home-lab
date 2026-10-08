"""Sealed run records: the JSON files a run leaves behind once, and never replaces."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_new(path: Path, text: str) -> None:
    """Write ``path`` whole or not at all, and never over an existing file: the text goes to a
    temporary file beside it, which is then linked into place (a link fails if the name
    exists)."""
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.link(staged, path)
    finally:
        os.unlink(staged)
