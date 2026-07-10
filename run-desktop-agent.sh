#!/bin/bash
set -a
source "$(dirname "$0")/.desktop-agent.env"
set +a
cd "$(dirname "$0")"
.venv/bin/python desktop_agent.py --poll-observe --poll-computer
