#!/bin/sh
# Read-only macOS prerequisite check for home-lab.
# Never calls sudo and never writes to the system.

set -u
missing=0
fail() { printf 'MISSING: %s\n' "$1"; missing=1; }
ok() { printf 'OK: %s\n' "$1"; }

printf '%s\n' 'home-lab prerequisite check'
printf '%s\n' '=========================='

if [ "$(uname -s)" = 'Darwin' ]; then
  # MLX, which serves the model, asks for macOS 14.0 or later (its install page).
  macos="$(sw_vers -productVersion 2>/dev/null || true)"
  case "${macos%%.*}" in
    ''|*[!0-9]*) printf '%s\n' 'INFO: could not read the macOS version; MLX needs 14.0 or later.' ;;
    *) if [ "${macos%%.*}" -ge 14 ]; then ok "macOS: $macos"; else fail "macOS 14 or later (detected: $macos)"; fi ;;
  esac
else
  fail 'macOS (this installer currently targets macOS)'
fi

arch="$(uname -m 2>/dev/null || true)"
if [ "$arch" = 'arm64' ]; then ok "Apple silicon: $arch"; else fail "Apple silicon (detected: ${arch:-unknown})"; fi

if command -v xcrun >/dev/null 2>&1 && xcrun --find clang >/dev/null 2>&1; then
  ok 'Xcode Command Line Tools'
else
  fail 'Xcode Command Line Tools (install with: xcode-select --install)'
fi

if command -v git >/dev/null 2>&1; then ok "Git: $(git --version)"; else fail Git; fi
if command -v uv >/dev/null 2>&1; then ok "uv: $(uv --version 2>/dev/null || printf installed)"; else fail 'uv (install it before running uv sync)'; fi

memory_gb=''
if command -v sysctl >/dev/null 2>&1; then
  memory_bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
  case "$memory_bytes" in
    ''|*[!0-9]*) ;;
    *) memory_gb=$((memory_bytes / 1024 / 1024 / 1024)) ;;
  esac
fi

if [ -n "$memory_gb" ]; then
  ok "unified memory: ${memory_gb} GB"
  case "$memory_gb" in
    16) printf '%s\n' 'MODEL TIER (untested): 16 GB: only the small Qwen3 4B Instruct 4-bit model; nothing larger has been tried at this size' ;;
    32) printf '%s\n' 'MODEL TIER (measured): 32 GB: Qwen3-Coder-30B-A3B-Instruct-4bit-DWQ' ;;
    64) printf '%s\n' 'MODEL TIER (untested): 64 GB: the 32 GB model works; benchmark anything larger before changing the default' ;;
    *) printf '%s\n' "MODEL TIER: no tested tier is recorded for ${memory_gb} GB; choose conservatively and benchmark first." ;;
  esac
else
  printf '%s\n' 'INFO: could not read hw.memsize; choose the model tier manually.'
fi

if [ -d "$HOME" ]; then
  free_kb="$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [ -n "$free_kb" ]; then
    free_gb=$((free_kb / 1024 / 1024))
    printf 'INFO: free space on HOME volume: %s GB\n' "$free_gb"
    if [ "$free_gb" -lt 30 ]; then fail 'at least 30 GB free on the volume containing HOME'; fi
  fi
fi

if [ "$missing" -ne 0 ]; then
  printf '%s\n' ''
  printf '%s\n' 'Prerequisite check FAILED. No changes were made.'
  exit 1
fi
printf '%s\n' ''
printf '%s\n' 'Prerequisite check PASSED. No changes were made.'
exit 0
