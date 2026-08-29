#!/bin/bash
# Overnight batch: generate fresh text cards, then quarry the source pool.
#
# NOTHING IS DELETED. No --delete-source, no prune, no tidy. Every phase only
# adds: new card rows, new rendered MP4s, new produced clips. Source material is
# left exactly as found, so a bad pass costs disk and nothing else.
set -u
LOG=/assets/.cache/overnight.log
LOCK=/assets/.cache/overnight.lock
mkdir -p /assets/.cache

# Single-instance guard. Two copies of this script writing the same SQLite file
# produce "database is locked" and lose generated cards — which happened once,
# when a pkill did not take and a relaunch left two runs racing each other.
if [ -e "$LOCK" ]; then
  if kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "already running as PID $(cat "$LOCK"); refusing to start a second" >>"$LOG"
    exit 1
  fi
  rm -f "$LOCK"          # stale lock from a killed run
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM

exec >>"$LOG" 2>&1
echo "=== START $(date -Is) ==="

echo "--- phase 1: generate text cards ---"
# Model-written kinds go to cards.py; grounded and dated kinds have their own
# generators. Routing mirrors /api/generate so both paths stay in step.
for kind in psa number trivia corrections achievements coming_up tiny_games; do
  echo "[cards] $kind (model)"
  timeout 900 python3 -m bumparr.generators.cards --kind "$kind" --n 12 2>&1 | tail -2
  sleep 3
done
for kind in trivia fun_facts; do
  echo "[cards] $kind (grounded)"
  timeout 900 python3 -m bumparr.generators.grounded --kind "$kind" --n 12 2>&1 | tail -2
  sleep 3
done
echo "[cards] on_this_day"
timeout 900 python3 -m bumparr.generators.on_this_day --n 12 2>&1 | tail -2

echo "--- phase 2: render every unrendered card to MP4 ---"
timeout 5400 python3 -m bumparr.render_cards 2>&1 | tail -6

echo "--- phase 3: quarry source video into branded clips ---"
# --per-source 5 keeps this first full pass reviewable: short stock still yields
# its natural 2, long archive films yield 5 rather than 40. No --delete-source.
timeout 21600 python3 -m bumparr.produce --per-source 5 --seed 19 2>&1 | tail -40

echo "--- phase 4: final counts (read-only) ---"
python3 -c "
import sqlite3
c=sqlite3.connect('/data/miketv.db')
for r in c.execute(\"SELECT type, COUNT(*) n FROM playables WHERE enabled=1 AND health='ok' GROUP BY type\"):
    print('  %-7s %d' % r)
print('  TOTAL', c.execute(\"SELECT COUNT(*) FROM playables WHERE enabled=1 AND health='ok'\").fetchone()[0])
"
echo "=== DONE $(date -Is) ==="
