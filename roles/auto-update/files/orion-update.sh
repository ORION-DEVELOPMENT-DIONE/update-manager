#!/bin/bash
LOG="/var/log/ansible-pull.log"
REPO="https://github.com/ORION-DEVELOPMENT-DIONE/update-manager.git"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') UPDATE CHECK ===" >> "$LOG"

# Run with -o (only if changed)
OUTPUT=$(ansible-pull -U "$REPO" -C main local.yml -i localhost, -o 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ] && [ -z "$OUTPUT" ]; then
    echo "No changes detected. Skipping." >> "$LOG"
elif [ $EXIT_CODE -eq 0 ]; then
    echo "$OUTPUT" >> "$LOG"
    echo "Update applied successfully." >> "$LOG"
else
    echo "$OUTPUT" >> "$LOG"
    echo "ERROR: ansible-pull exited with code $EXIT_CODE" >> "$LOG"
fi

echo "=== END ===" >> "$LOG"