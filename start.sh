#!/bin/bash
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
export DISPLAY=:99
exec python app.py
