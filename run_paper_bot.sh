#!/usr/bin/env bash
cd "$(dirname "$0")"
mkdir -p paper
echo $$ > paper/supervisor.pid
trap 'kill $CHILD 2>/dev/null; echo "$(date "+%F %T") supervisor stopped" >> paper/supervisor.log; exit 0' TERM INT
while true; do
  python3 paper_bot.py >> paper/bot_stdout.log 2>&1 &
  CHILD=$!; echo $CHILD > paper/bot.pid
  echo "$(date '+%F %T') started paper_bot pid=$CHILD" >> paper/supervisor.log
  sleep 60
  N=0
  while kill -0 $CHILD 2>/dev/null; do
    sleep 2; N=$((N+1)); [ $((N % 15)) -ne 0 ] && continue
    HB=$(python3 -c "import json,time;print(int(time.time()-json.load(open('paper/heartbeat.json'))['ts']))" 2>/dev/null || echo 999)
    if [ "$HB" -gt 120 ]; then echo "$(date '+%F %T') heartbeat stale ${HB}s, killing $CHILD" >> paper/supervisor.log; kill $CHILD; sleep 5; kill -9 $CHILD 2>/dev/null; fi
  done
  echo "$(date '+%F %T') paper_bot pid=$CHILD exited; restarting in 1s" >> paper/supervisor.log
  sleep 1
done
