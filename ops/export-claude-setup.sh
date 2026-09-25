#!/usr/bin/env bash
# Package the portable parts of this machine's Claude Code setup.
#
# Run on the SOURCE machine (the MacBook Air). Produces a tarball you
# copy to the Mac mini and unpack with restore-claude-setup.sh.
#
# What this deliberately does NOT include:
#   * credentials     - auth lives in the macOS Keychain, not in
#                       ~/.claude, so it cannot travel in a tarball.
#                       You run `claude login` on the mini instead.
#   * plugins/cache   - ~1 GB of installed plugin code. Claude Code
#                       reinstalls it from the marketplaces listed in
#                       settings.json, so shipping it is pure waste.
#   * skills/gstack   - 1.1 GB git clone. Re-clone it on the mini if
#                       you want it.
#   * sessions, history, logs, telemetry, caches - machine-local noise.
#
set -euo pipefail

SRC="${HOME}/.claude"
OUT="${1:-${HOME}/claude-setup-$(date +%Y%m%d).tar.gz}"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

echo "Staging portable Claude Code setup..."
mkdir -p "${STAGE}/claude-setup"

# --- the rules ---------------------------------------------------------
cp "${SRC}/CLAUDE.md" "${STAGE}/claude-setup/"
if [ -d "${SRC}/rules" ]; then
  cp -R "${SRC}/rules" "${STAGE}/claude-setup/"
fi

# --- skills, minus the giant re-clonable one ---------------------------
if [ -d "${SRC}/skills" ]; then
  mkdir -p "${STAGE}/claude-setup/skills"
  # --exclude gstack: 1.1 GB git clone, re-clone on the target instead.
  tar -cf - -C "${SRC}" --exclude='skills/gstack' skills \
    | tar -xf - -C "${STAGE}/claude-setup/../"
  rm -rf "${STAGE}/claude-setup/skills"
  mv "${STAGE}/skills" "${STAGE}/claude-setup/skills"
fi

# --- settings, with machine-specific context stripped ------------------
# settings.json carries two different kinds of thing: portable config
# (model, enabled plugins, marketplaces, theme) and per-machine state
# (autoMode's learned environment notes, which describe THIS machine's
# projects and would be actively wrong on the mini). Keep the first,
# drop the second.
python3 - "${SRC}/settings.json" "${STAGE}/claude-setup/settings.json" <<'PY'
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh:
    settings = json.load(fh)

dropped = []
for key in ("autoMode", "statusLine"):
    if key in settings:
        settings.pop(key)
        dropped.append(key)

with open(dst, "w", encoding="utf-8") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")

if dropped:
    print(f"  stripped machine-specific keys: {', '.join(dropped)}")
PY

# --- global MCP servers ------------------------------------------------
# These live in ~/.claude.json alongside a lot of per-machine state, so
# extract just the server definitions.
if [ -f "${HOME}/.claude.json" ]; then
  python3 - "${HOME}/.claude.json" "${STAGE}/claude-setup/mcp-servers.json" <<'PY'
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh:
    config = json.load(fh)

servers = config.get("mcpServers", {})
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(servers, fh, indent=2)
    fh.write("\n")
print(f"  captured {len(servers)} global MCP server(s): "
      f"{', '.join(servers) or 'none'}")
PY
fi

# --- project memory (optional but worth carrying) ----------------------
# The mini works on the same projects, so the working state is useful
# there. Skip it with SKIP_MEMORY=1 if you would rather start clean.
if [ "${SKIP_MEMORY:-0}" != "1" ] && [ -d "${SRC}/projects" ]; then
  mkdir -p "${STAGE}/claude-setup/projects"
  # Only the memory directories, not session transcripts.
  (cd "${SRC}/projects" && find . -type d -name memory -print0) \
    | while IFS= read -r -d '' dir; do
        mkdir -p "${STAGE}/claude-setup/projects/${dir}"
        cp -R "${SRC}/projects/${dir}/." \
              "${STAGE}/claude-setup/projects/${dir}/"
      done
  echo "  included project memory directories"
fi

cp "$(dirname "$0")/restore-claude-setup.sh" "${STAGE}/claude-setup/" \
  2>/dev/null || true

tar -czf "${OUT}" -C "${STAGE}" claude-setup
echo
echo "Wrote ${OUT}"
du -h "${OUT}" | cut -f1 | sed 's/^/  size: /'
echo
echo "Copy it to the mini, then run restore-claude-setup.sh there."
