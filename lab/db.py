"""Read-only connections to the lab's database (#386).

SQLite reads a ``file:`` name as a URI. A ``?`` or ``#`` in the path ends the
file name early, and ``%XX`` is decoded. A read-only open built from the raw
path could then open a different file, and the ``mode=ro`` after the ``?``
would be lost. So the path is turned into a percent-encoded file URI first.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def connect_readonly(path: Path | str, **kwargs: Any) -> sqlite3.Connection:
    """Open the database file at ``path`` read-only.

    SQLite does not create the file, and no migration runs. Extra keyword
    arguments, such as ``timeout``, go to ``sqlite3.connect``.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn: sqlite3.Connection = sqlite3.connect(uri, uri=True, **kwargs)
    return conn
