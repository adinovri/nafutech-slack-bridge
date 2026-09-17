#!/usr/bin/env bash
# uninstall_mac.sh — undo what run_mac.sh installed.
#
# Usage:
#   bash uninstall_mac.sh              # bootout + remove plists (SAFE)
#   bash uninstall_mac.sh --purge      # + wipe .venv/ and logs/ from the repo
#   bash uninstall_mac.sh --dry-run    # preview what would happen, do nothing
#
# What this script NEVER touches, by design:
#   - .env                             (your secrets — remove by hand if you want)
#   - the repo itself                  (git-tracked files are yours)
#   - ~/.openclaw/                     (shared registry across the OpenClaw ecosystem;
#                                       delete manually if you're SURE nothing else uses it)
#   - ~/.config/nafutech-slack-bridge.env   (optional Telegram creds)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

MODE="stop"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --purge)   MODE="purge" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

say()  { printf '\n\033[1;36m▶\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ============================================================================
# 1. Sanity
# ============================================================================
[ "$(uname -s)" = "Darwin" ] || die "this is the macOS uninstaller — on Linux run  bash uninstall_linux.sh"

if ! command -v launchctl >/dev/null 2>&1; then
  warn "launchctl not found — nothing to uninstall on the launchd side; will fall through to file cleanup"
  HAS_LAUNCHCTL=0
else
  HAS_LAUNCHCTL=1
fi

# ============================================================================
# 2. Bootout + remove plists
# ============================================================================
say "unloading LaunchAgents"

uninstall_plist() {
  local plist_name="$1"
  local dest="$HOME/Library/LaunchAgents/$plist_name"

  if [ "$HAS_LAUNCHCTL" = "1" ] && [ -e "$dest" ]; then
    # `|| true` — bootout errors ("service not loaded") are fine here.
    run launchctl bootout "gui/$(id -u)" "$dest" 2>/dev/null || true
    ok "bootout $plist_name"
  fi
  if [ -e "$dest" ]; then
    run rm -f "$dest"
    ok "removed $plist_name"
  fi
}

uninstall_plist "io.nanovest.nafutech-slack-bridge.plist"
uninstall_plist "io.nanovest.nafu-bg-watchdog.plist"

# ============================================================================
# 3. Optional --purge: repo-local runtime dirs
# ============================================================================
if [ "$MODE" = "purge" ]; then
  say "purging repo-local runtime artifacts (--purge)"
  if [ -d .venv ]; then
    run rm -rf .venv
    ok "removed .venv/"
  fi
  if [ -d logs ]; then
    run rm -rf logs
    ok "removed logs/"
  fi
  run find src -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
fi

# ============================================================================
# 4. Summary
# ============================================================================
say "done"
echo "──────────────────────────────────────────────────"
echo "Removed:  LaunchAgents (bridge + [bg] watchdog)"
if [ "$MODE" = "purge" ]; then
  echo "Purged:   .venv/, logs/, src/**/__pycache__"
fi
echo
echo "Kept (delete by hand if you want them gone):"
echo "  .env                                     (secrets)"
echo "  ~/.openclaw/bg_registry.json             (shared bg-task registry)"
echo "  ~/.config/nafutech-slack-bridge.env      (optional Telegram env, if you created one)"
echo "  the repo itself, git history"
echo "──────────────────────────────────────────────────"
if [ "$DRY_RUN" = "1" ]; then
  warn "dry-run — nothing was actually removed"
fi
