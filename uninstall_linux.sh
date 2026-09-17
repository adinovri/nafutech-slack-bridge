#!/usr/bin/env bash
# uninstall_linux.sh — undo what run_linux.sh installed.
#
# Usage:
#   bash uninstall_linux.sh              # stop + disable services, remove unit files (SAFE)
#   bash uninstall_linux.sh --purge      # + wipe .venv/ and logs/ from the repo
#   bash uninstall_linux.sh --dry-run    # preview what would happen, do nothing
#
# What this script NEVER touches, by design:
#   - .env                             (your secrets — remove by hand if you want)
#   - the repo itself                  (git-tracked files are yours)
#   - ~/.openclaw/                     (shared registry across the OpenClaw ecosystem;
#                                       delete manually if you're SURE nothing else uses it)
#   - loginctl linger                  (per-user policy — leave as-is)
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
[ "$(uname -s)" = "Linux" ] || die "this is the Linux uninstaller — on macOS run  bash uninstall_mac.sh"

if ! command -v systemctl >/dev/null 2>&1; then
  warn "systemctl not found — nothing to uninstall on the systemd side; will fall through to file cleanup"
  HAS_SYSTEMCTL=0
else
  HAS_SYSTEMCTL=1
fi

# ============================================================================
# 2. Stop + disable services
# ============================================================================
if [ "$HAS_SYSTEMCTL" = "1" ]; then
  say "stopping + disabling systemd services"
  # `|| true` on every one so an already-gone unit doesn't abort the rest.
  run systemctl --user disable --now nafutech-slack-bridge.service 2>/dev/null || true
  ok "bridge stopped + disabled"
  run systemctl --user disable --now nafu-bg-watchdog.timer 2>/dev/null || true
  ok "watchdog timer stopped + disabled"
  run systemctl --user stop            nafu-bg-watchdog.service 2>/dev/null || true
  ok "watchdog service stopped"
fi

# ============================================================================
# 3. Remove unit files
# ============================================================================
say "removing unit files from ~/.config/systemd/user/"
for f in nafutech-slack-bridge.service nafu-bg-watchdog.service nafu-bg-watchdog.timer; do
  path="$HOME/.config/systemd/user/$f"
  if [ -e "$path" ]; then
    run rm -f "$path"
    ok "removed $f"
  fi
done

if [ "$HAS_SYSTEMCTL" = "1" ]; then
  run systemctl --user daemon-reload
  # Prune any lingering "loaded" state for units that no longer exist.
  run systemctl --user reset-failed 2>/dev/null || true
  ok "systemd reloaded"
fi

# ============================================================================
# 4. Optional --purge: repo-local runtime dirs
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
  # Note: __pycache__ is regenerated on next run; still worth clearing.
  run find src -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
fi

# ============================================================================
# 5. Summary
# ============================================================================
say "done"
echo "──────────────────────────────────────────────────"
echo "Removed:  systemd units (bridge + watchdog + timer)"
if [ "$MODE" = "purge" ]; then
  echo "Purged:   .venv/, logs/, src/**/__pycache__"
fi
echo
echo "Kept (delete by hand if you want them gone):"
echo "  .env                                     (secrets)"
echo "  ~/.openclaw/bg_registry.json             (shared bg-task registry)"
echo "  ~/.config/nafutech-slack-bridge.env      (optional Telegram env, if you created one)"
echo "  the repo itself, git history, systemd linger flag"
echo "──────────────────────────────────────────────────"
if [ "$DRY_RUN" = "1" ]; then
  warn "dry-run — nothing was actually removed"
fi
