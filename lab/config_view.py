"""Secret-safe, read-only snapshot of configured environment variable presence.

Only fixed, documented names are reported. Values are deliberately never
returned, even when they appear harmless, because an operator may accidentally
put a credential, chat ID or token in any variable.
"""

from __future__ import annotations

from collections.abc import Mapping

# Names reviewed against the environment-variable list in ops/release.md.
# New variables must be reviewed and added explicitly, not discovered from
# the process environment. In particular do not enumerate LAB_SECRET_* names.
CONFIG_NAMES: tuple[str, ...] = (
    "LAB_ALERT_KIND",
    "LAB_BACKUP_DIR",
    "LAB_CHAT_ID",
    "LAB_CONTAINER_IMAGE",
    "LAB_LOG_DIR",
    "LAB_LOG_LEVEL",
    "LAB_MCP_SERVERS",
    "LAB_MODEL_NAME",
    "LAB_MODEL_REVISION",
    "LAB_MODEL_TOKENIZER_REVISION",
    "LAB_MODEL_URL",
    "LAB_MODEL_WEIGHTS_MB",
    "LAB_OPERATOR_KEY",
    "LAB_OPERATOR_PUBKEY",
    "LAB_REPO_SOURCES",
    "LAB_TARGET",
    "LAB_WEB_FETCH_HOSTS",
)


def snapshot(environ: Mapping[str, str]) -> dict[str, str]:
    """Report presence, never raw environment values or unknown variable names.

    An empty value is treated as unset. Secret variables appear only as a
    single group, so even their suffixes cannot reveal project information.
    A set variable is not necessarily valid or used by a running daemon.
    """
    result = {name: "set" if environ.get(name) else "unset" for name in CONFIG_NAMES}
    result["LAB_SECRET_*"] = (
        "set" if any(key.startswith("LAB_SECRET_") and value
                     for key, value in environ.items()) else "unset"
    )
    return dict(sorted(result.items()))
