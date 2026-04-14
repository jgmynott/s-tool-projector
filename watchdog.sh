#!/bin/bash
# Sentiment backtest watchdog — runs every 30 min via launchd or manual loop
# Checks if collector is alive, restarts if dead, reports progress

PROJECT_DIR="$HOME/Documents/Claude/s2tool-projector"
CSV="$PROJECT_DIR/sentiment_data/sentiment_combined_2023-01-01_2025-01-01.csv"
LOG="$PROJECT_DIR/sentiment_run.log"
WATCHLOG="$PROJECT_DIR/watchdog.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') — Watchdog check" >> "$WATCHLOG"

# Count rows (subtract 1 for header)
if [ -f "$CSV" ]; then
    ROWS=$(( $(wc -l < "$CSV") - 1 ))
else
    ROWS=0
fi

# Estimate progress: ~730 days, avg ~72 ticker-days per day = ~52,560 target rows
# More conservatively, check the last date processed
LAST_LINE=$(tail -1 "$LOG" 2>/dev/null)
LAST_DATE=$(grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2}:' "$LOG" 2>/dev/null | tail -1 | tr -d ':')

if [ -n "$LAST_DATE" ]; then
    # Calculate days processed
    START_SEC=$(date -j -f "%Y-%m-%d" "2023-01-01" "+%s" 2>/dev/null)
    LAST_SEC=$(date -j -f "%Y-%m-%d" "$LAST_DATE" "+%s" 2>/dev/null)
    if [ -n "$START_SEC" ] && [ -n "$LAST_SEC" ]; then
        DAYS_DONE=$(( (LAST_SEC - START_SEC) / 86400 ))
        PCT=$(( DAYS_DONE * 100 / 730 ))
    else
        DAYS_DONE="?"
        PCT="?"
    fi
else
    DAYS_DONE="?"
    PCT="?"
fi

echo "  Progress: $ROWS rows, ~$DAYS_DONE days done, ~${PCT}% complete" >> "$WATCHLOG"

# Check if process is running
if pgrep -f "sentiment_collector.py.*--all.*--start 2023-01-01" > /dev/null 2>&1; then
    echo "  Status: RUNNING (PID $(pgrep -f 'sentiment_collector.py.*--all.*--start 2023-01-01'))" >> "$WATCHLOG"
else
    # Check if it's actually done (last date is 2024-12-31 or later)
    if [ "$DAYS_DONE" != "?" ] && [ "$DAYS_DONE" -ge 728 ]; then
        echo "  Status: COMPLETE! All days processed." >> "$WATCHLOG"
        echo "$(date '+%Y-%m-%d %H:%M:%S') — BACKTEST COMPLETE ($ROWS rows, $DAYS_DONE days)" >> "$WATCHLOG"
        exit 0
    fi

    echo "  Status: DEAD — restarting..." >> "$WATCHLOG"
    cd "$PROJECT_DIR"
    nohup python3 sentiment_collector.py --all --start 2023-01-01 --end 2025-01-01 --resume >> "$LOG" 2>&1 &
    NEW_PID=$!
    echo "  Restarted with PID $NEW_PID" >> "$WATCHLOG"
fi

echo "" >> "$WATCHLOG"
