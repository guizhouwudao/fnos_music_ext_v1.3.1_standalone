#!/usr/bin/env bash
set -u

echo "[watchdog] fnmusic persistent watchdog started..."
while true; do
    sleep 3
    if [ -S /var/run/trim_music.socket ]; then
        PID=$(fuser /var/run/trim_music.socket 2>/dev/null | tr -d ' ' || true)
        if [ -n "${PID}" ]; then
            EXE=$(readlink -f "/proc/${PID}/exe" 2>/dev/null || true)
            if [[ "${EXE}" =~ "trim-music" ]]; then
                echo "[watchdog] trim.music restart detected (PID=${PID}). Re-attaching fnmusic-ext..."
                systemctl restart fnmusic-ext.service || true
                sleep 5
            fi
        fi
    fi
done
