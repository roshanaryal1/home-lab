#!/usr/bin/env bash
# Install the same marketplaces and plugins this setup uses.
#
# Run on the Mac mini AFTER installing Claude Code and running
# `claude login`. Generated from the source machine's actual
# installed_plugins.json and known_marketplaces.json, not hand-typed.
#
# Safe to re-run: adding a marketplace or installing a plugin that is
# already present is a no-op.
set -uo pipefail

fail=0

echo "== Adding marketplaces =="

echo "  caveman <- JuliusBrussee/caveman"
claude plugin marketplace add "JuliusBrussee/caveman" || { echo "    FAILED: caveman"; fail=1; }
echo "  claude-plugins-official <- anthropics/claude-plugins-official"
claude plugin marketplace add "anthropics/claude-plugins-official" || { echo "    FAILED: claude-plugins-official"; fail=1; }
echo "  ecc <- https://github.com/affaan-m/ECC.git"
claude plugin marketplace add "https://github.com/affaan-m/ECC.git" || { echo "    FAILED: ecc"; fail=1; }
echo "  headroom-marketplace <- headroomlabs-ai/headroom"
claude plugin marketplace add "headroomlabs-ai/headroom" || { echo "    FAILED: headroom-marketplace"; fail=1; }
echo "  last30days-skill <- mvanhorn/last30days-skill"
claude plugin marketplace add "mvanhorn/last30days-skill" || { echo "    FAILED: last30days-skill"; fail=1; }
echo "  mattpocock <- mattpocock/skills"
claude plugin marketplace add "mattpocock/skills" || { echo "    FAILED: mattpocock"; fail=1; }
echo "  thecolab-skills <- thecolab-ai/.skills"
claude plugin marketplace add "thecolab-ai/.skills" || { echo "    FAILED: thecolab-skills"; fail=1; }
echo "  thedotmack <- thedotmack/claude-mem"
claude plugin marketplace add "thedotmack/claude-mem" || { echo "    FAILED: thedotmack"; fail=1; }
echo "  ui-ux-pro-max-skill <- nextlevelbuilder/ui-ux-pro-max-skill"
claude plugin marketplace add "nextlevelbuilder/ui-ux-pro-max-skill" || { echo "    FAILED: ui-ux-pro-max-skill"; fail=1; }

echo
echo "== Installing plugins =="
echo "  caveman@caveman"
claude plugin install "caveman@caveman" -y || { echo "    FAILED: caveman@caveman"; fail=1; }
echo "  claude-mem@thedotmack"
claude plugin install "claude-mem@thedotmack" -y || { echo "    FAILED: claude-mem@thedotmack"; fail=1; }
echo "  ecc@ecc"
claude plugin install "ecc@ecc" -y || { echo "    FAILED: ecc@ecc"; fail=1; }
echo "  frontend-design@claude-plugins-official"
claude plugin install "frontend-design@claude-plugins-official" -y || { echo "    FAILED: frontend-design@claude-plugins-official"; fail=1; }
echo "  github@claude-plugins-official"
claude plugin install "github@claude-plugins-official" -y || { echo "    FAILED: github@claude-plugins-official"; fail=1; }
echo "  headroom@headroom-marketplace"
claude plugin install "headroom@headroom-marketplace" -y || { echo "    FAILED: headroom@headroom-marketplace"; fail=1; }
echo "  last30days@last30days-skill"
claude plugin install "last30days@last30days-skill" -y || { echo "    FAILED: last30days@last30days-skill"; fail=1; }
echo "  mattpocock-skills@mattpocock"
claude plugin install "mattpocock-skills@mattpocock" -y || { echo "    FAILED: mattpocock-skills@mattpocock"; fail=1; }
echo "  nz-skills@thecolab-skills"
claude plugin install "nz-skills@thecolab-skills" -y || { echo "    FAILED: nz-skills@thecolab-skills"; fail=1; }
echo "  pr-review-toolkit@claude-plugins-official"
claude plugin install "pr-review-toolkit@claude-plugins-official" -y || { echo "    FAILED: pr-review-toolkit@claude-plugins-official"; fail=1; }
echo "  pyright-lsp@claude-plugins-official"
claude plugin install "pyright-lsp@claude-plugins-official" -y || { echo "    FAILED: pyright-lsp@claude-plugins-official"; fail=1; }
echo "  semgrep@claude-plugins-official"
claude plugin install "semgrep@claude-plugins-official" -y || { echo "    FAILED: semgrep@claude-plugins-official"; fail=1; }
echo "  superpowers@claude-plugins-official"
claude plugin install "superpowers@claude-plugins-official" -y || { echo "    FAILED: superpowers@claude-plugins-official"; fail=1; }
echo "  typescript-lsp@claude-plugins-official"
claude plugin install "typescript-lsp@claude-plugins-official" -y || { echo "    FAILED: typescript-lsp@claude-plugins-official"; fail=1; }
echo "  ui-ux-pro-max@ui-ux-pro-max-skill"
claude plugin install "ui-ux-pro-max@ui-ux-pro-max-skill" -y || { echo "    FAILED: ui-ux-pro-max@ui-ux-pro-max-skill"; fail=1; }
echo "  vercel@claude-plugins-official"
claude plugin install "vercel@claude-plugins-official" -y || { echo "    FAILED: vercel@claude-plugins-official"; fail=1; }

echo
if [ "$fail" -ne 0 ]; then
  echo "One or more steps failed. Re-run, or install the failures by hand."
  exit 1
fi
echo "All marketplaces and plugins installed. Restart claude to load them."

