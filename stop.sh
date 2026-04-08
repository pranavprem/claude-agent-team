#!/usr/bin/env bash
# stop.sh — Kill the Neo tmux session and all its processes.
tmux kill-session -t neo 2>/dev/null && echo "[neo] Session stopped." || echo "[neo] No active session."
