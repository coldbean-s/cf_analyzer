#!/bin/bash
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
XVFB_PID=$!
sleep 1
if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb failed to start, retrying..."
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
    Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
    sleep 1
fi
export DISPLAY=:99
exec python app.py
