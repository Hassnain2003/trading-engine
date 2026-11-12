#!/bin/bash
# Keeps the agent alive on a laptop (no systemd): restarts it whenever it
# exits, including the engine's deliberate self-restart (exit 42) after
# persistent network errors. On the VPS, systemd's Restart=always does this
# job instead — don't run both.
cd "$(dirname "$0")/.." || exit 1
source venv/bin/activate

# Exponential backoff on rapid exits: a flapping network otherwise causes a
# restart (and an ntfy alert) every ~12 minutes all night. Runs that survive
# >30 min reset the backoff.
delay=20
while true; do
    start=$(date +%s)
    python3 live/agent_runner.py
    code=$?
    if [ "$code" -eq 3 ]; then
        echo "$(date -Is) another agent instance holds agent.lock — this wrapper exits"
        exit 0
    fi
    ran=$(( $(date +%s) - start ))
    if [ "$ran" -gt 1800 ]; then
        delay=20
    else
        delay=$(( delay * 3 ))
        [ "$delay" -gt 900 ] && delay=900
    fi
    echo "$(date -Is) agent exited (code=$code, ran ${ran}s) — restarting in ${delay}s"
    sleep "$delay"
done
