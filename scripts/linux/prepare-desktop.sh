#!/usr/bin/env bash
# Run as root after prepare-host.sh; services are enabled separately.
set -euo pipefail
test "$(id -u)" = 0
dnf install -y xorg-x11-server-Xvfb openbox xterm x11vnc novnc python3-websockify \
  xorg-x11-xauth xorg-x11-utils wqy-microhei-fonts at-spi2-atk gtk3 alsa-lib libXScrnSaver mesa-libgbm
/opt/agenttracekit/venv/bin/pip install playwright==1.63.0
runuser -u atk -- env PLAYWRIGHT_BROWSERS_PATH=/home/atk/.cache/ms-playwright \
  /opt/agenttracekit/venv/bin/playwright install --no-shell chromium
