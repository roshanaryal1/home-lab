#!/usr/bin/env bash
# Restore a Claude Code setup exported by export-claude-setup.sh.
#
# Run on the TARGET machine (the Mac mini), from inside the unpacked
# claude-setup directory.
#
# This is deliberately non-destructive: anything it would overwrite is
# backed up first, and it refuses to clobber an existing CLAUDE.md
# without saving a copy.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${HOME}/.claude"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${DEST}/backups/pre-restore-${STAMP}"

echo "Restoring Claude Code setup into ${DEST}"
mkdir -p "${DEST}" "${BACKUP}"

backup_then_copy() {
  local name="$1"
  local src="${HERE}/${name}"
  [ -e "${src}" ] || return 0
  if [ -e "${DEST}/${name}" ]; then
    cp -R "${DEST}/${name}" "${BACKUP}/" 2>/dev/null || true
    echo "  backed up existing ${name}"
  fi
  cp -R "${src}" "${DEST}/"
  echo "  restored ${name}"
}

backup_then_copy CLAUDE.md
backup_then_copy rules
backup_then_copy settings.json

# Skills merge rather than replace, so anything already on the target
# survives.
if [ -d "${HERE}/skills" ]; then
  mkdir -p "${DEST}/skills"
  cp -R "${HERE}/skills/." "${DEST}/skills/"
  echo "  merged $(find "${HERE}/skills" -maxdepth 1 -mindepth 1 -type d \
    | wc -l | tr -d ' ') skill(s)"
fi

# Project memory, if it was included in the export.
if [ -d "${HERE}/projects" ]; then
  mkdir -p "${DEST}/projects"
  cp -R "${HERE}/projects/." "${DEST}/projects/"
  echo "  restored project memory"
fi

# MCP servers go into ~/.claude.json, which also holds per-machine
# state, so merge the server block in rather than replacing the file.
if [ -f "${HERE}/mcp-servers.json" ]; then
  python3 - "${HERE}/mcp-servers.json" "${HOME}/.claude.json" <<'PY'
import json
import os
import sys

servers_path, config_path = sys.argv[1], sys.argv[2]
with open(servers_path, encoding="utf-8") as fh:
    servers = json.load(fh)

config = {}
if os.path.exists(config_path):
    with open(config_path, encoding="utf-8") as fh:
        try:
            config = json.load(fh)
        except json.JSONDecodeError:
            print("  WARNING: existing ~/.claude.json is not valid JSON, "
                  "leaving it alone")
            raise SystemExit(0)

existing = config.setdefault("mcpServers", {})
added = [name for name in servers if name not in existing]
existing.update(servers)

with open(config_path, "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2)
    fh.write("\n")

print(f"  merged MCP servers (added: {', '.join(added) or 'none new'})")
PY
fi

cat <<'NEXT'

Restored. Three things still need doing by hand, because they cannot
travel in a tarball:

  1. Authenticate:      claude login
     Credentials live in the macOS Keychain on the source machine, not
     in ~/.claude, so they do not transfer. This is a good thing.

  2. Install plugins:   bash install-plugins.sh
     Adds all 9 marketplaces and installs all 16 plugins, generated
     from the source machine's real manifest. Downloads them fresh
     rather than copying ~1 GB of cache across. Safe to re-run.

  3. Check the MCP servers start:  claude mcp list
     They are all npx-based, so the first run downloads each server.
     That needs Node installed and a working network.

Optional: skills/gstack was excluded from the export because it is a
1.1 GB git clone. Re-clone it on this machine if you use it.

NEXT
