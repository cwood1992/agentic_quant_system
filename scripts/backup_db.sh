#!/bin/bash
# Backup the SQLite database with a timestamp.
# Removes backups older than 7 days.
#
# Usage:
#   bash scripts/backup_db.sh [db_path] [backup_dir]
#
# Defaults:
#   db_path:    data/system.db
#   backup_dir: data/backups

set -euo pipefail

DB_PATH="${1:-data/system.db}"
BACKUP_DIR="${2:-data/backups}"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/system_${TIMESTAMP}.db"

# Validate source exists
if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database not found at $DB_PATH"
    exit 1
fi

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Use SQLite's .backup command for a consistent copy (handles WAL mode)
if command -v sqlite3 &> /dev/null; then
    sqlite3 "$DB_PATH" ".backup '${BACKUP_FILE}'"
else
    # Fallback to file copy if sqlite3 CLI not available
    cp "$DB_PATH" "$BACKUP_FILE"
    # Also copy WAL and SHM if they exist
    [ -f "${DB_PATH}-wal" ] && cp "${DB_PATH}-wal" "${BACKUP_FILE}-wal"
    [ -f "${DB_PATH}-shm" ] && cp "${DB_PATH}-shm" "${BACKUP_FILE}-shm"
fi

echo "Backup created: $BACKUP_FILE"

# Remove backups older than 7 days
find "$BACKUP_DIR" -name "system_*.db*" -mtime +7 -delete 2>/dev/null || true

REMAINING=$(find "$BACKUP_DIR" -name "system_*.db" | wc -l)
echo "Backups retained: $REMAINING"
