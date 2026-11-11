#!/bin/bash
set -a
source "$(dirname "$0")/../.desktop-agent.env"
set +a
cd "$(dirname "$0")/.."
.venv/bin/python -m conveyor.entrypoints.desktop_agent --poll-observe --poll-computer
