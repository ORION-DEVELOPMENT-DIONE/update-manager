#!/bin/bash
# Orion auto-update — runs daily at 03:00 via cron.
# Triggers if remote commit on tracked branch is >= STALE_DAYS old and
# device is behind. Rolls back on health-check failure.

set -uo pipefail

LOG=/var/log/orion-update.log
APP_DIR=/home/orangepi/screen-manager
APP_USER=orangepi
SERVICE=screen.service
STALE_DAYS=5
MIN_DISK_MB=500
FAILURE_FILE=/var/lib/orion/auto-update-failures
LOCK=/var/run/orion-update.lock

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [AUTO] $*"  | tee -a "$LOG"; }
err() { echo "$(date '+%Y-%m-%d %H:%M:%S') [AUTO] ERROR: $*" | tee -a "$LOG" >&2; }

mkdir -p "$(dirname "$FAILURE_FILE")"

# ── Lock — prevent concurrent runs (manual + auto) ────────────────────────────
exec 200>"$LOCK"
flock -n 200 || { log "Another update in progress — skipping"; exit 0; }

# ── Guard: failure loop (3+ failures in 24h → stop) ───────────────────────────
if [ -f "$FAILURE_FILE" ]; then
    now=$(date +%s)
    recent=$(awk -v cutoff=$((now - 86400)) '$1 >= cutoff' "$FAILURE_FILE" | wc -l)
    if [ "$recent" -ge 3 ]; then
        err "Aborting — $recent failures in last 24h. Manual intervention required."
        exit 1
    fi
fi

# ── Guard: disk space ─────────────────────────────────────────────────────────
free_mb=$(df -m "$APP_DIR" | awk 'NR==2 {print $4}')
if [ "$free_mb" -lt "$MIN_DISK_MB" ]; then
    err "Aborting — only ${free_mb}MB free, need ${MIN_DISK_MB}MB"
    echo "$(date +%s) disk_full" >> "$FAILURE_FILE"
    exit 1
fi

# ── Guard: app dir is a git repo ──────────────────────────────────────────────
if [ ! -d "$APP_DIR/.git" ]; then
    err "Aborting — $APP_DIR is not a git repo"
    exit 1
fi

cd "$APP_DIR" || { err "cannot cd $APP_DIR"; exit 1; }

# ── Fetch remote state ────────────────────────────────────────────────────────
branch=$(sudo -u "$APP_USER" git rev-parse --abbrev-ref HEAD 2>/dev/null)
[ -z "$branch" ] && { err "Could not detect branch"; exit 1; }

if ! sudo -u "$APP_USER" git fetch origin "$branch" >>"$LOG" 2>&1; then
    err "git fetch failed — network issue. Will retry tomorrow."
    exit 0   # not a "failure" worth counting toward loop guard
fi

local_hash=$(sudo -u "$APP_USER" git rev-parse HEAD)
remote_hash=$(sudo -u "$APP_USER" git rev-parse "origin/$branch")

# ── Already current ───────────────────────────────────────────────────────────
if [ "$local_hash" = "$remote_hash" ]; then
    log "Already on latest ($local_hash) — no action"
    exit 0
fi

# ── Age check ─────────────────────────────────────────────────────────────────
remote_ts=$(sudo -u "$APP_USER" git log -1 --format=%ct "$remote_hash")
now=$(date +%s)
age_days=$(( (now - remote_ts) / 86400 ))

log "branch=$branch local=$local_hash remote=$remote_hash commit_age=${age_days}d threshold=${STALE_DAYS}d"

if [ "$age_days" -lt "$STALE_DAYS" ]; then
    log "Update available but only ${age_days}d old — waiting for manual update"
    exit 0
fi

# ── Guard: service must be running before we start ────────────────────────────
if ! systemctl is-active --quiet "$SERVICE"; then
    err "Aborting — $SERVICE not active before update. Won't auto-restart a stopped service."
    exit 1
fi

# ── Perform update ────────────────────────────────────────────────────────────
log "Starting auto-update: $local_hash → $remote_hash (age ${age_days}d)"

if ! sudo -u "$APP_USER" git pull --ff-only origin "$branch" >>"$LOG" 2>&1; then
    err "git pull --ff-only failed"
    echo "$(date +%s) git_pull_failed" >> "$FAILURE_FILE"
    exit 1
fi

new_hash=$(sudo -u "$APP_USER" git rev-parse HEAD)
log "Code updated → $new_hash — restarting $SERVICE"

if ! systemctl restart "$SERVICE"; then
    err "systemctl restart failed — rolling back to $local_hash"
    sudo -u "$APP_USER" git reset --hard "$local_hash" >>"$LOG" 2>&1
    systemctl restart "$SERVICE" || true
    echo "$(date +%s) restart_failed" >> "$FAILURE_FILE"
    exit 1
fi

# ── Health check ──────────────────────────────────────────────────────────────
log "Waiting 30s for service to stabilize..."
sleep 30

if systemctl is-active --quiet "$SERVICE"; then
    log "SUCCESS — service healthy on $new_hash"
    : > "$FAILURE_FILE"   # clear on success
    exit 0
fi

# ── Rollback ──────────────────────────────────────────────────────────────────
err "Service failed health check — rolling back to $local_hash"
sudo -u "$APP_USER" git reset --hard "$local_hash" >>"$LOG" 2>&1
systemctl restart "$SERVICE" || true
sleep 10

if systemctl is-active --quiet "$SERVICE"; then
    log "Rollback OK — back on $local_hash"
    echo "$(date +%s) bad_commit_rolled_back" >> "$FAILURE_FILE"
    exit 1
fi

err "CRITICAL — rollback also failed. Service down. Manual intervention required."
echo "$(date +%s) rollback_failed" >> "$FAILURE_FILE"
exit 2