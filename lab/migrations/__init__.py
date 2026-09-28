"""Versioned schema migrations (item 3.1, #64).

``CREATE TABLE IF NOT EXISTS`` never alters a table that already exists,
so a database written by an older build kept its old shape and the code
that came after it failed on the first missing column. Here every schema
change is a numbered SQL file in this directory, ``NNNN_name.sql``,
applied in order, each in one transaction, with the number recorded in
``PRAGMA user_version`` in the same transaction. A migration either
happened completely or not at all, and the file itself says which.

Rules for a migration file:

* Never edit one that has shipped. Add the next number.
* No PRAGMAs and no BEGIN/COMMIT; the runner owns both.
* Rebuilding a table (SQLite cannot add a CHECK to an existing one) is
  fine: the runner turns foreign keys off around each migration and
  checks them again before it commits.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """A migration failed and was rolled back; the database is unchanged."""


class SchemaTooNew(MigrationError):
    """The database was written by a newer build than this one."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring a database from before versioning up to the baseline shape.

    Those databases carry version 0 and the tables the baseline script
    leaves alone because they already exist. Three columns arrived after
    the first release; add whichever is absent. The names interpolated
    below are the literals in this tuple; DDL cannot bind parameters.
    """
    for table, column, ddl in (
        ("leases", "generation", "INTEGER NOT NULL DEFAULT 0"),
        ("approvals", "intent", "TEXT"),
        ("tasks", "executions", "INTEGER NOT NULL DEFAULT 0"),
    ):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # nosemgrep
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")  # nosemgrep


# Python that must run right after a migration's SQL, in the same
# transaction. Only for what SQL cannot say, such as "add this column
# unless it is already there".
AFTER: dict[int, Callable[[sqlite3.Connection], None]] = {1: _add_missing_columns}


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """The migrations in ``directory``, in order, with no gaps."""
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE.match(path.name)
        if match is None:
            raise MigrationError(f"{path.name} is not named NNNN_name.sql")
        found.append(Migration(int(match.group(1)), match.group(2), path))
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"migrations must be numbered 1, 2, 3 with no gaps; "
                f"found {migration.path.name} where {expected:04d} belongs"
            )
    return found


def split_statements(script: str) -> list[str]:
    """Split a script into statements without ``executescript``.

    ``executescript`` commits any open transaction first, which would
    end the migration's transaction before it finished. SQLite's own
    ``complete_statement`` knows about strings, comments and trigger
    bodies, so this stays correct for ``CREATE TRIGGER ... BEGIN ... END;``.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    leftover = [ln for ln in buffer.splitlines()
                if ln.strip() and not ln.strip().startswith("--")]
    if leftover:
        raise MigrationError(f"script ends in an unterminated statement: {leftover[0]!r}")
    return statements


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    migrations = discover(directory)
    return migrations[-1].version if migrations else 0


def _apply(conn: sqlite3.Connection, migration: Migration) -> bool:
    """Apply one migration atomically. False if another process beat us to it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Two processes can open the same fresh file together. The one that
        # waited on the write lock must look again before it repeats work.
        if current_version(conn) >= migration.version:
            conn.execute("ROLLBACK")
            return False
        for statement in split_statements(migration.path.read_text(encoding="utf-8")):
            conn.execute(statement)
        hook = AFTER.get(migration.version)
        if hook is not None:
            hook(conn)
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise MigrationError(
                f"{len(broken)} foreign key violation(s) after migration "
                f"{migration.version:04d}_{migration.name}"
            )
        # PRAGMA cannot bind parameters; the version is an int from a filename.
        conn.execute(f"PRAGMA user_version = {int(migration.version)}")  # nosemgrep
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, MigrationError) or not isinstance(exc, Exception):
            raise
        raise MigrationError(
            f"migration {migration.version:04d}_{migration.name} failed and was "
            f"rolled back: {type(exc).__name__}: {exc}"
        ) from exc
    return True


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> int:
    """Bring ``conn``'s database to the latest version; return that version.

    The connection must be in autocommit mode (``isolation_level=None``).
    Raises ``SchemaTooNew`` rather than run against a schema this build
    does not understand.
    """
    migrations = discover(directory)
    latest = migrations[-1].version if migrations else 0
    version = current_version(conn)
    if version > latest:
        raise SchemaTooNew(
            f"database is at schema version {version}, this build knows up to {latest}"
        )
    for migration in migrations[version:]:
        # Off for the whole migration, so a table rebuild is not blocked by
        # the tables that reference it. Checked again before the commit.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            _apply(conn, migration)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    return current_version(conn)
