#!/bin/bash
exec xvfb-run --auto-servernum --server-args="-screen 0 1366x768x24 -nolisten tcp" python app.py
