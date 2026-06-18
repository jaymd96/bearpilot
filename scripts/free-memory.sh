#!/usr/bin/env bash
# free-memory.sh — Quit memory-hungry apps before running Ollama locally.
#
# Usage:
#   bash scripts/free-memory.sh          # Quit all listed apps
#   bash scripts/free-memory.sh --dry    # Just show what would be quit + memory saved
#
# Safe to run anytime — uses osascript "quit app" which lets apps
# save state gracefully (not kill -9).

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry" ]] && DRY_RUN=true

# Apps to quit, ordered by typical memory usage.
# Edit this list to match your workflow.
APPS=(
    "Google Chrome"
    "Microsoft Teams"
    "Claude"
    "Notion"
    "Loom"
    "OrbStack"
    "Keynote"
    "Slack"
)

total_freed=0

for app in "${APPS[@]}"; do
    # Check if the app is running via pgrep on the .app bundle name
    if pgrep -f "${app}.app" > /dev/null 2>&1; then
        # Estimate memory: sum RSS of all processes matching this app
        mb=$(ps -eo rss,comm | grep "${app}" | awk '{sum+=$1} END {printf "%.0f", sum/1024}')
        if $DRY_RUN; then
            printf "  would quit %-25s  (~%s MB)\n" "$app" "$mb"
        else
            osascript -e "quit app \"${app}\"" 2>/dev/null && \
                printf "  quit %-25s  (~%s MB freed)\n" "$app" "$mb" || \
                printf "  skip %-25s  (not running or refused)\n" "$app"
        fi
        total_freed=$((total_freed + mb))
    fi
done

if [[ $total_freed -eq 0 ]]; then
    echo "Nothing to quit — all target apps already closed."
else
    if $DRY_RUN; then
        printf "\nWould free ~%s MB. Run without --dry to quit them.\n" "$total_freed"
    else
        printf "\nFreed ~%s MB. Waiting 3s for processes to exit...\n" "$total_freed"
        sleep 3
        # Show current memory state
        free_pct=$(memory_pressure 2>/dev/null | grep "free percentage" | awk '{print $NF}')
        echo "System memory free: ${free_pct:-unknown}"
    fi
fi
