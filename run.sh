#!/bin/bash
# The one way to start the server: right folder, right python.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "no venv - one-time setup:"
    echo "  python3 -m venv --system-site-packages .venv"
    echo "  .venv/bin/pip install -r requirements.txt"
    exit 1
fi
exec .venv/bin/python server.py
