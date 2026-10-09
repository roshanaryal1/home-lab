"""``lab security-audit``: one read-only check of the lab's own boundary (#362).

The lab's safety rests on which OS account can read and write what. The agent runs
as a non-admin ``lab`` account that cannot write the deployed code, the service
definitions or the configuration, and cannot read the operator's private key. This
command checks those permissions on an install. Each check gives one finding,
``ok``, ``FAIL`` or ``skip``, and the offending paths of a FAIL.

It is read-only. It uses ``os.lstat`` and ``os.scandir`` only. It never follows a
symlink, never reads a file's contents and never changes a mode or an owner. Every
path and every expected owner is a parameter with the Mac mini's value as the
default, so the tests run on temporary folders.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lab import accountplan
from lab.doctor import LOOPBACK_HOSTS

DEPLOY_ROOT = Path("/opt/homelab")
LAUNCH_DAEMONS = Path(accountplan.LAUNCH_DAEMONS)
CONFIG_DIR = Path(accountplan.CONFIG_DIR)
DATA_DIR = Path(accountplan.DATA_DIR)
DATABASE_NAME = "lab.db"
SERVICE_PLIST = "com.homelab.*.plist"
OPERATOR_KEY = Path(".lab-operator", "operator.key")
GROUP_OR_OTHER_WRITE = 0o022
ANY_GROUP_OR_OTHER = 0o077

Status = Literal["ok", "FAIL", "skip"]


@dataclass(frozen=True)
class Finding:
    name: str
    status: Status
    paths: list[str]


def _lstat(path: Path) -> os.stat_result | None:
    """``os.lstat`` of ``path``, or None when it cannot be read."""
    try:
        return os.lstat(path)
    except OSError:
        return None


def _is_dir(st: os.stat_result | None) -> bool:
    # The lstat of a symlink is a link, not a directory, so a link is never walked.
    return st is not None and stat.S_ISDIR(st.st_mode)


def _trusted(path: Path, uid: int, mask: int) -> bool:
    """True when ``path`` is not a symlink, is owned by ``uid`` and has no bit of ``mask``.

    A symlink is never trusted, because the file behind it would be read unchecked.
    """
    st = _lstat(path)
    return (st is not None and not stat.S_ISLNK(st.st_mode) and st.st_uid == uid
            and (st.st_mode & mask) == 0)


def _result(name: str, bad: Iterable[str]) -> Finding:
    paths = sorted(set(bad))
    status: Status = "FAIL" if paths else "ok"
    return Finding(name, status, paths)


def check_deploy(root: Path, root_uid: int) -> Finding:
    """Every entry under ``root``, the root included, is owned by ``root_uid`` and has no
    group or other write bit. Symlinks are listed by their folder, never judged, never
    followed. A root that is missing or is itself a symlink fails."""
    if not _is_dir(_lstat(root)):
        return Finding("deploy", "FAIL", [str(root)])
    bad: set[str] = set()
    pending = [root]
    while pending:
        path = pending.pop()
        st = _lstat(path)
        if st is None:
            bad.add(str(path))
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        unsafe = st.st_uid != root_uid or (st.st_mode & GROUP_OR_OTHER_WRITE) != 0
        if stat.S_ISDIR(st.st_mode):
            try:
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
            except OSError:
                unsafe = True
        if unsafe:
            bad.add(str(path))
    return _result("deploy", bad)


def check_services(launch_daemons: Path, root_uid: int) -> Finding:
    """Every ``com.homelab.*.plist`` is owned by ``root_uid`` with no group or other write
    bit, and at least one exists."""
    if not _is_dir(_lstat(launch_daemons)):
        return Finding("services", "FAIL", [str(launch_daemons)])
    try:
        with os.scandir(launch_daemons) as entries:
            plists = sorted(entry.path for entry in entries
                            if fnmatch.fnmatchcase(entry.name, SERVICE_PLIST))
    except OSError:
        return Finding("services", "FAIL", [str(launch_daemons)])
    if not plists:
        return Finding("services", "FAIL", [])
    return _result("services", [plist for plist in plists
                                if not _trusted(Path(plist), root_uid, GROUP_OR_OTHER_WRITE)])


def check_config(config_dir: Path, root_uid: int) -> Finding:
    """``config_dir`` and each entry directly in it are owned by ``root_uid`` with no group or
    other write bit. An entry that is a symlink fails, because its target would be read
    unchecked."""
    if not _is_dir(_lstat(config_dir)):
        return Finding("config", "FAIL", [str(config_dir)])
    bad: set[str] = set()
    if not _trusted(config_dir, root_uid, GROUP_OR_OTHER_WRITE):
        bad.add(str(config_dir))
    try:
        with os.scandir(config_dir) as entries:
            children = [Path(entry.path) for entry in entries]
    except OSError:
        bad.add(str(config_dir))
        children = []
    bad.update(str(child) for child in children
               if not _trusted(child, root_uid, GROUP_OR_OTHER_WRITE))
    return _result("config", bad)


def check_data(data_dir: Path) -> Finding:
    """``data_dir`` and the database in it have the same owner, and neither is open to a group
    or to others. A missing one fails."""
    database = data_dir / DATABASE_NAME
    dir_st = _lstat(data_dir)
    db_st = _lstat(database)
    bad: set[str] = set()
    if not _is_dir(dir_st):
        bad.add(str(data_dir))
    if db_st is None or not stat.S_ISREG(db_st.st_mode):
        bad.add(str(database))
    if dir_st is not None and db_st is not None:
        if db_st.st_uid != dir_st.st_uid:
            bad.add(str(database))
        if (dir_st.st_mode & ANY_GROUP_OR_OTHER) != 0:
            bad.add(str(data_dir))
        if (db_st.st_mode & ANY_GROUP_OR_OTHER) != 0:
            bad.add(str(database))
    return _result("data", bad)


def check_public_key(env: Mapping[str, str], root_uid: int) -> Finding:
    """The file named by LAB_OPERATOR_PUBKEY is owned by ``root_uid`` with no group or other
    write bit. When the variable is not set the check fails with no path."""
    value = env.get("LAB_OPERATOR_PUBKEY", "").strip()
    if not value:
        return Finding("public_key", "FAIL", [])
    path = Path(value)
    trusted = _trusted(path, root_uid, GROUP_OR_OTHER_WRITE)
    return _result("public_key", [] if trusted else [str(path)])


def check_private_key(home: Path, euid: int, data_dir: Path) -> Finding:
    """``~/.lab-operator/operator.key`` is a regular file owned by ``euid``, owned by a
    different account from the data folder, and closed to any group or other. It is skipped
    when this account cannot see the file at all."""
    key = home / OPERATOR_KEY
    try:
        st = os.lstat(key)
    except (PermissionError, FileNotFoundError):
        return Finding("private_key", "skip", [])
    except OSError:
        return Finding("private_key", "FAIL", [str(key)])
    data_st = _lstat(data_dir)
    unsafe = (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != euid
        or data_st is None
        or st.st_uid == data_st.st_uid
        or (st.st_mode & ANY_GROUP_OR_OTHER) != 0
    )
    return _result("private_key", [str(key)] if unsafe else [])


def check_loopback(env: Mapping[str, str]) -> Finding:
    """When LAB_MODEL_URL is set, its host is loopback, so the model server is not exposed.
    Not set is ok, since there is nothing to expose."""
    url = env.get("LAB_MODEL_URL", "").strip()
    if not url:
        return Finding("loopback", "ok", [])
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        host = None
    status: Status = "ok" if host in LOOPBACK_HOSTS else "FAIL"
    return Finding("loopback", status, [])


def run(env: Mapping[str, str], *, home: Path | None = None, deploy_root: Path = DEPLOY_ROOT,
        launch_daemons: Path = LAUNCH_DAEMONS, config_dir: Path = CONFIG_DIR,
        data_dir: Path = DATA_DIR, root_uid: int = 0, euid: int | None = None) -> list[Finding]:
    """Every check, in the order they print. Reads ``env`` and the permissions, writes nothing.

    ``home`` defaults to this account's home folder and ``euid`` to this process's effective
    uid. ``root_uid`` is the owner the root-owned paths must have.
    """
    return [
        check_deploy(deploy_root, root_uid),
        check_services(launch_daemons, root_uid),
        check_config(config_dir, root_uid),
        check_data(data_dir),
        check_public_key(env, root_uid),
        check_private_key(Path.home() if home is None else home,
                          os.geteuid() if euid is None else euid, data_dir),
        check_loopback(env),
    ]
