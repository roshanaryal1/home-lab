"""Versioned schema migrations (item 3.1, #64).

``CREATE TABLE IF NOT EXISTS`` never alters a table that already exists,
so a database written by an older build kept its old shape and the code
that came after it failed on the first missing column. Here every schema
change is a numbered SQL file in this directory, ``NNNN_name.sql``,
applied in order, each in one transaction, with the number recorded in
``PRAGMA user_version`` in the same transaction. A migration either
happened completely or not at all, and the file itself says which.

Before a database file below the latest version is upgraded, ``migrate``
copies the file beside itself as ``<name>.pre-vN.bak`` with SQLite's online
backup API, and deletes the older copies of that file. A database from before
versioning (version 0 with tables) gets one too. A fresh database, empty at
version 0, and ``:memory:`` get none.

Rules for a migration file:

* Never edit one that has shipped. Add the next number, and add its line to
  ``SHA256SUMS`` (``tests/test_migration_checksums.py`` fails until you do).
* No PRAGMAs and no BEGIN/COMMIT; the runner owns both.
* Rebuilding a table (SQLite cannot add a CHECK to an existing one) is
  fine: the runner turns foreign keys off around each migration and
  checks them again before it commits.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import tempfile
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


def online_copy(source: sqlite3.Connection, dest: Path) -> None:
    """Copy ``source`` into the file ``dest`` with SQLite's online backup API.

    The copy is left in rollback-journal mode, so it is one self-contained
    file even when the source is in WAL mode. ``lab.backup`` uses it too.
    """
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
    finally:
        target.close()


def _database_file(conn: sqlite3.Connection) -> Path | None:
    """The file behind the main database, or None for ``:memory:``."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def _check_snapshot(path: Path, version: int) -> None:
    """A snapshot is kept only if it is intact and at the version its name gives."""
    check = sqlite3.connect(path)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        copied = current_version(check)
    finally:
        check.close()
    if integrity != "ok" or copied != version:
        raise MigrationError(
            f"the snapshot of version {version} is not usable ({integrity}, "
            f"version {copied}), so no migration was run"
        )


def _prune_snapshots(db: Path, version: int) -> None:
    """Delete the ``<name>.pre-vN.bak`` files beside ``db`` with N below ``version``,
    regular files only.

    Only older copies go. A process that copied version 5 and was slow to get
    here must not delete the version 6 copy that another process made after
    upgrading the file further.
    """
    pattern = re.compile(re.escape(db.name) + r"\.pre-v(\d+)\.bak")
    with os.scandir(db.parent) as entries:
        for entry in entries:
            match = pattern.fullmatch(entry.name)
            if (match and int(match.group(1)) < version
                    and entry.is_file(follow_symlinks=False)):
                # Another process opening the same file may have pruned it first.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(entry.path)


def _has_tables(conn: sqlite3.Connection) -> bool:
    """True when the database holds a table of its own, so version 0 is not a fresh file."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite!_%' ESCAPE '!' LIMIT 1").fetchone() is not None


def _needs_snapshot(conn: sqlite3.Connection, version: int, latest: int) -> bool:
    """An upgrade of a file with data in it: any version below the latest, except
    a version 0 file with no tables, which is a fresh one."""
    return version < latest and (version > 0 or _has_tables(conn))


def _snapshot_before_upgrade(conn: sqlite3.Connection, latest: int) -> None:
    """Copy the database beside itself as ``<name>.pre-vN.bak``, before it is upgraded.

    Only a file ``_needs_snapshot`` accepts is copied. The version is read
    inside the read transaction the copy is taken in, so
    the copy is exactly the version its name gives, even when another
    process upgrades the file first. A read transaction does not block
    writers in WAL mode.
    """
    db = _database_file(conn)
    if db is None:
        return
    conn.execute("BEGIN")
    try:
        version = current_version(conn)
        if not _needs_snapshot(conn, version, latest):
            return
        final = db.with_name(f"{db.name}.pre-v{version}.bak")
        fd, name = tempfile.mkstemp(dir=db.parent, prefix=f".{final.name}.",
                                    suffix=".partial")
        os.close(fd)
        partial = Path(name)
        try:
            online_copy(conn, partial)
            _check_snapshot(partial, version)
            with open(partial, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(partial, final)
            dir_fd = os.open(db.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    finally:
        conn.execute("ROLLBACK")
    _prune_snapshots(db, version)


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> int:
    """Bring ``conn``'s database to the latest version; return that version.

    The connection must be in autocommit mode (``isolation_level=None``).
    Raises ``SchemaTooNew`` rather than run against a schema this build
    does not understand. Before the first upgrade of a file database, a
    snapshot is written beside it. If that fails, nothing is migrated.
    """
    migrations = discover(directory)
    latest = migrations[-1].version if migrations else 0
    version = current_version(conn)
    if version > latest:
        raise SchemaTooNew(
            f"database is at schema version {version}, this build knows up to {latest}"
        )
    if _needs_snapshot(conn, version, latest):
        try:
            _snapshot_before_upgrade(conn, latest)
        except (OSError, sqlite3.Error) as exc:
            raise MigrationError(
                f"could not snapshot the database before upgrading it, so nothing "
                f"was migrated: {exc}"
            ) from exc
    for migration in migrations[version:]:
        # Off for the whole migration, so a table rebuild is not blocked by
        # the tables that reference it. Checked again before the commit.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            _apply(conn, migration)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    return current_version(conn)
